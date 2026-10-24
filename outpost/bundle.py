"""Apply private worker exports transactionally without dropping unknown fields."""
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from . import model
from .util import canonical,check_database,connect,copy_file,digest,relative,write_json


def stream(root,name):
    path=root/relative(name)
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():raise ValueError('Unsafe or missing bundle stream')
    with path.open() as f:
        for number,line in enumerate(f,1):
            if not line.strip():continue
            row=json.loads(line)
            if not isinstance(row,dict):raise ValueError('Bundle rows must be JSON objects')
            yield row


def _checked_file(root,entry,reference_roots=()):
    declared=Path(entry['path'])
    candidates=[]
    if not declared.is_absolute():
        candidates.append(root/relative(entry['path']))
    else:candidates.append(declared)
    original=Path(entry.get('original_path',entry['path']))
    if original.is_absolute():candidates.append(original)
    for allowed in reference_roots:
        if not declared.is_absolute():candidates.append(allowed/relative(entry['path']))
        if not original.is_absolute():candidates.append(allowed/relative(str(original)))
    path=None
    for candidate in candidates:
        target=candidate.resolve()
        permitted=target.is_relative_to(root) or any(target==allowed or target.is_relative_to(allowed) for allowed in reference_roots)
        if permitted and target.is_file():
            path=target;break
    if path is None:raise ValueError('Unsafe or missing bundle file; external references require explicit approved roots')
    if digest(path)!=entry['sha256']:raise ValueError('Bundle file checksum mismatch')
    if entry.get('bytes') is not None and path.stat().st_size!=entry['bytes']:raise ValueError('Bundle file size mismatch')
    if path.name.lower().startswith(('.env','.dev.vars')) or path.suffix.lower() in ('.pem','.key','.p12','.pfx'):raise ValueError('Credential files cannot be imported')
    return path


def _screen_file(path,fmt):
    if fmt=='json':
        if model.credentials(json.loads(path.read_text())):raise ValueError('Credential-bearing source JSON; preserve it locally and review exact fields')
    elif fmt=='jsonl':
        for row in stream(path.parent,path.name):
            if model.credentials(row):raise ValueError('Credential-bearing source JSONL')
    elif fmt=='sqlite':
        if path.with_name(path.name+'-wal').exists() and path.with_name(path.name+'-wal').stat().st_size:raise ValueError('Raw catalog must be a consistent closed SQLite snapshot')
        db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True);db.row_factory=sqlite3.Row
        try:
            check_database(db)
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                quoted='"'+table.replace('"','""')+'"'
                for row in db.execute('SELECT * FROM '+quoted):
                    if model.credentials(dict(row)):raise ValueError('Credential-bearing raw catalog; source retained')
        finally:db.close()
    else:
        from .util import inspect_file
        reason=inspect_file(path,business=True)
        if reason:raise ValueError('Credential pattern in source file; review locally')


def _immutable(db,table,key,row):
    existing=db.execute(f'SELECT * FROM "{table}" WHERE "key"=?',(key,)).fetchone()
    if existing:
        if any(existing[k]!=v for k,v in row.items()):raise ValueError('Conflicting immutable source point or relationship')
        return
    columns=list(row)
    db.execute(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',[row[k] for k in columns])


def import_bundle(lib,app,root,dry_run=False,seed_catalog=None,reference_roots=None):
    root=Path(root).expanduser().resolve();path=lib.app_dir(app)
    reference_roots=[Path(p).expanduser().resolve() for p in (reference_roots or [])]
    manifest_path=root/'bundle.json';manifest=json.loads(manifest_path.read_text())
    if manifest.get('app_id')!=app or manifest.get('version',manifest.get('format_version'))!=1:raise ValueError('Bundle app or format mismatch')
    ident=manifest['bundle_id']
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}',ident):raise ValueError('Unsafe bundle ID')
    streams={k:manifest[k] for k in ('records','entities','metrics','relationships','asset_links') if manifest.get(k)}
    entries=[dict(e,_kind=kind) for kind in ('assets','raw_sources') for e in manifest.get(kind,[])]
    source_hashes={name:digest(root/relative(name)) for name in streams.values()}
    fingerprint=hashlib.sha256(canonical({'manifest':manifest,'streams':source_hashes})).hexdigest()
    prior=lib.rows(app,'source_imports',{'bundle_id':ident})
    if prior:
        if prior[0]['manifest_sha256']!=fingerprint:raise ValueError('Bundle ID already imported with changed input')
        return dict(json.loads(prior[0]['report_json']),already_imported=True)
    stats={k:0 for k in streams}
    keys={k:set() for k in ('records','entities','metrics','relationships')}
    for name,file in streams.items():
        for row in stream(root,file):
            if row.get('app',app)!=app:raise ValueError('Cross-app source record')
            if model.credentials(row):raise ValueError('Actual credentials found in bundle record; values withheld')
            if name=='records':
                key=row.get('record_id')
                stored_hash=hashlib.sha256(canonical(row.get('payload'))).hexdigest()
                expected=row.get('stored_payload_sha256',row['raw_payload_sha256'])
                if not key or stored_hash!=expected:raise ValueError('Source record identity or stored payload hash mismatch')
                if expected!=row['raw_payload_sha256'] and (not row.get('redacted_fields') or not row.get('redaction_pointers')):raise ValueError('Altered source payload requires explicit redaction provenance')
            elif name=='entities':key=row.get('entity_key')
            else:key=row.get('key') or (hashlib.sha256(canonical(row)).hexdigest() if name=='relationships' else None)
            if name in keys:
                if not key or key in keys[name]:raise ValueError('Duplicate or missing source identity')
                keys[name].add(key)
            stats[name]+=1
    resolved={}
    for entry in entries:
        source=_checked_file(root,entry,reference_roots);resolved[entry['path']]=source
        fmt=entry.get('format') or ('json' if source.suffix.lower()=='.json' else 'jsonl' if source.suffix.lower()=='.jsonl' else 'binary')
        try:_screen_file(source,fmt)
        except ValueError as e:raise ValueError('Bundle source screening failed for '+entry['path']+': '+str(e)) from e
    report=dict(stats,app_id=app,bundle_id=ident,files=len(entries),bytes=sum(resolved[e['path']].stat().st_size for e in entries),excluded=manifest.get('excluded',[]),sanitized=manifest.get('sanitized',[]),dry_run=dry_run)
    if dry_run:return report
    with lib.writer(app):
        if seed_catalog:
            from .library import initialize,counts
            with connect(path/'catalog.sqlite3',readonly=True) as old:
                if any(counts(old).values()) or old.execute('SELECT count(*) FROM vault_assets').fetchone()[0]:raise ValueError('Seed catalog requires an empty app')
            seed=Path(seed_catalog).resolve()
            with connect(seed,readonly=True) as src:
                if src.execute('PRAGMA journal_mode').fetchone()[0]!='delete':raise ValueError('Seed catalog must be a closed DELETE-journal export')
                if model.available(src)-set(model.PROVIDER_TABLES)-{'sqlite_sequence'}:raise ValueError('Unexpected seed-catalog scope')
                check_database(src);src.execute('BEGIN')
                temp=path/('.seed-'+uuid.uuid4().hex+'.sqlite3');copy_file(seed,temp)
            initialize(temp);os.replace(temp,path/'catalog.sqlite3')
        db=connect(path/'catalog.sqlite3')
        try:
            model.prepare(db);db.execute('BEGIN');db.execute('PRAGMA defer_foreign_keys=ON')
            db.execute('INSERT INTO source_imports VALUES (?,?,?,?)',(ident,fingerprint,json.dumps(report,sort_keys=True),canonical(manifest).decode()))
            for row in stream(root,streams['entities']):
                values=[row.get(k) for k in ('entity_key','kind','platform','source_id','account_key','parent_key','title','body','source_url','published_at')]+[canonical(row).decode()]
                old=db.execute('SELECT kind,platform,source_id FROM source_entities WHERE entity_key=?',(row['entity_key'],)).fetchone()
                if old and tuple(old)!=(row.get('kind'),row.get('platform'),row.get('source_id')):raise ValueError('Source entity identity collision')
                db.execute('INSERT INTO source_entities VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_key) DO UPDATE SET account_key=excluded.account_key,parent_key=excluded.parent_key,title=excluded.title,body=excluded.body,source_url=excluded.source_url,published_at=excluded.published_at,metadata_json=excluded.metadata_json',values)
            for row in stream(root,streams['records']):
                header=canonical({k:v for k,v in row.items() if k!='payload'}).decode();old=db.execute('SELECT raw_payload_sha256,header_json FROM source_events WHERE record_id=?',(row['record_id'],)).fetchone()
                if old:
                    if tuple(old)!=(row['raw_payload_sha256'],header):raise ValueError('Conflicting immutable source record')
                    continue
                payload=canonical(row['payload']).decode();base_key=(row.get('outer_fields') or {}).get('record_key',row['record_id'])
                base=db.execute('SELECT payload_json FROM records WHERE record_key=?',(base_key,)).fetchone()
                reference=None
                if base and json.loads(base[0])==row['payload']:reference=base_key;payload=None
                context=row.get('capture_context') or {}
                captured=context.get('captured_at') or context.get('observed_at')
                db.execute('INSERT INTO source_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(row['record_id'],row['kind'],row.get('platform'),row.get('entity_id'),row.get('entity_key'),row.get('source_path'),row.get('source_sha256'),row.get('source_coordinate'),captured,canonical(context).decode(),payload,reference,row['raw_payload_sha256'],header))
            for row in stream(root,streams['metrics']):
                value=row.get('value');number=float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(float(value)) else None
                data={'key':row['key'],'record_id':row.get('record_id'),'entity_key':row.get('entity_key'),'captured_at':row.get('captured_at'),'metric':row['metric'],'value_json':canonical(value).decode(),'value_number':number,'value_text':value if isinstance(value,str) else None,'unit':row.get('unit'),'source_coordinate':row.get('source_coordinate'),'raw_json':canonical(row).decode()}
                _immutable(db,'source_metrics',row['key'],data)
            for row in stream(root,streams['relationships']):
                key=row.get('key') or hashlib.sha256(canonical(row)).hexdigest()
                data={k:row.get(k) for k in ('from_key','to_key','relationship','record_id')};data.update(key=key,raw_json=canonical(row).decode());_immutable(db,'source_relationships',key,data)
            for entry in entries:
                source=resolved[entry['path']];sha=entry['sha256'];asset=db.execute('SELECT stored_path FROM vault_assets WHERE sha256=?',(sha,)).fetchone()
                original=db.execute('SELECT stored_path FROM vault_file_provenance WHERE stored_sha256=? LIMIT 1',(sha,)).fetchone()
                stored=asset[0] if asset else original[0] if original else 'files/assets/'+sha
                destination=path/relative(stored)
                if destination.is_symlink() or not destination.resolve().is_relative_to(path):raise ValueError('Bundle file destination escapes app')
                if not destination.exists():
                    destination.parent.mkdir(parents=True,exist_ok=True);copy_file(source,destination)
                    import shutil
                    shutil.copystat(source,destination)
                if not destination.resolve().is_relative_to(path) or digest(destination)!=sha:raise ValueError('Stored bundle file checksum/containment mismatch')
                db.execute('INSERT OR IGNORE INTO vault_assets VALUES (?,?,?,?,?,?)',(sha,sha,stored,source.stat().st_size,entry.get('media_kind','document'),canonical(entry.get('provenance',{})).decode()))
                original_path=entry.get('original_path',entry['path'])
                db.execute('INSERT OR IGNORE INTO vault_asset_sources VALUES (?,?)',(original_path,sha))
                metadata={k:v for k,v in entry.items() if k!='_kind'}
                metadata.update(staged_mode=source.stat().st_mode & 0o777,staged_mtime_ns=source.stat().st_mtime_ns)
                db.execute('INSERT INTO source_import_files VALUES (?,?,?,?,?,?,?,?,?)',(ident,original_path,stored,sha,entry.get('original_sha256',sha),source.stat().st_size,entry['_kind'],entry.get('format'),canonical(metadata).decode()))
            if 'asset_links' in streams:
                for row in stream(root,streams['asset_links']):
                    key=row.get('key') or hashlib.sha256(canonical(row)).hexdigest();sha=row.get('sha256',row.get('asset_sha256'))
                    if not sha and row.get('source_path'):
                        original=row['source_path']
                        linked=db.execute('SELECT sha256 FROM source_import_files WHERE bundle_id=? AND source_path=?',(ident,original)).fetchone()
                        if not linked and not Path(original).is_absolute():
                            candidates=list(db.execute('SELECT sha256 FROM source_import_files WHERE bundle_id=? AND source_path LIKE ?',(ident,'%/'+original)))
                            if len(candidates)==1:linked=candidates[0]
                        if not linked and not Path(original).is_absolute():linked=db.execute('SELECT stored_sha256 FROM vault_file_provenance WHERE stored_path=?',('files/legacy/'+original,)).fetchone()
                        if linked:sha=linked[0]
                    data={'key':key,'record_id':row.get('record_id'),'entity_key':row.get('entity_key'),'sha256':sha,'role':row.get('role',row.get('relationship')),'ordinal':row.get('ordinal',row.get('position',0)),'raw_json':canonical(row).decode()};_immutable(db,'source_asset_links',key,data)
            folder=path/'files/imports'/ident
            if folder.is_symlink() or not folder.resolve().is_relative_to(path):raise ValueError('Bundle archive escapes app')
            folder.mkdir(parents=True,exist_ok=True)
            mp=folder/'bundle.json'
            if mp.is_symlink():raise ValueError('Unsafe bundle manifest destination')
            if mp.exists() and json.loads(mp.read_text())!=manifest:raise ValueError('An interrupted bundle with different input is retained; use a new bundle ID')
            for name,file in streams.items():
                destination=folder/(name+'.jsonl.gz')
                if destination.is_symlink():raise ValueError('Unsafe bundle archive destination')
                if not destination.exists():
                    temp=folder/('.stream-'+uuid.uuid4().hex)
                    with (root/file).open('rb') as src,temp.open('xb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0,compresslevel=1) as dst:
                        import shutil
                        shutil.copyfileobj(src,dst)
                    os.link(temp,destination);temp.unlink()
                h=hashlib.sha256()
                with gzip.open(destination,'rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                if h.hexdigest()!=source_hashes[file]:raise ValueError('Retained bundle stream checksum mismatch')
                db.execute('INSERT INTO source_import_files VALUES (?,?,?,?,?,?,?,?,?)',(ident,'stream:'+name,destination.relative_to(path).as_posix(),digest(destination),source_hashes[file],destination.stat().st_size,'stream','jsonl+gzip',canonical({'input_path':file,'input_bytes':(root/file).stat().st_size}).decode()))
            if not mp.exists():write_json(mp,manifest)
            check_database(db);db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()
    return report
