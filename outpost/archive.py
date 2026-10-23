"""Consistent, content-addressed snapshots and fresh-directory restores."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .library import PATH_COLUMNS, counts, screen_catalog
from .util import (backup_database, canonical, check_database, connect, copy_entries,
                   digest, inventory, lock, relative, slug, write_json)


def snapshot_id(value):
    if not re.fullmatch('[0-9a-f]{64}', value or ''):
        raise ValueError('Snapshot ID must be its full SHA-256')
    return value


def snapshot_path(lib, app, value):
    return lib.app_dir(app)/'snapshots'/snapshot_id(value)


def file_entry(path, rel):
    st=path.stat()
    return {'path':rel,'kind':'file','sha256':digest(path),'size':st.st_size,
            'mode':st.st_mode & 0o777,'mtime_ns':st.st_mtime_ns}


def validate_manifest(manifest):
    if manifest.get('format_version') != 1:
        raise ValueError('Unsupported snapshot format')
    slug(manifest['app_id'])
    ident=snapshot_id(manifest['snapshot_id'])
    payload={k:v for k,v in manifest.items() if k!='snapshot_id'}
    if hashlib.sha256(canonical(payload)).hexdigest()!=ident:
        raise ValueError('Manifest checksum mismatch')
    seen=set()
    for entry in manifest['entries']:
        rel=str(relative(entry['path']))
        if rel in seen or rel in ('manifest.json','COMMITTED.json'):
            raise ValueError('Duplicate or reserved manifest path')
        if rel not in ('app.json','catalog.sqlite3') and not rel.startswith('files/'):
            raise ValueError('Unexpected manifest path')
        seen.add(rel)
        if entry['kind']=='file':
            snapshot_id(entry['sha256'])
            if entry['size']<0:
                raise ValueError('Invalid file size')
        elif entry['kind']=='symlink':
            target=entry['target']
            if not target or Path(target).is_absolute() or '\\' in target or '\0' in target:
                raise ValueError('Unsafe symlink target')
        else:
            raise ValueError('Invalid file kind')
        if not isinstance(entry['mode'],int) or not 0<=entry['mode']<=0o777:
            raise ValueError('Invalid permission mode')
        if not isinstance(entry['mtime_ns'],int) or entry['mtime_ns']<0:
            raise ValueError('Invalid file timestamp')
    if not {'app.json','catalog.sqlite3'}<=seen:
        raise ValueError('Snapshot lacks app identity or catalog')
    for rel in seen:
        if any('/'.join(rel.split('/')[:n]) in seen for n in range(1,len(rel.split('/')))):
            raise ValueError('File or symlink is also a parent path')
    return manifest


def check_paths(db, root):
    for table,columns in dict(PATH_COLUMNS,vault_assets=('stored_path',)).items():
        for col in columns:
            for (value,) in db.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL AND "{col}" != \'\''):
                p=root/relative(value)
                if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():
                    raise ValueError(f'Catalog references missing or unsafe file in {table}.{col}')


def verify_snapshot(source):
    source=Path(source).resolve()
    mp=source/'manifest.json'
    if mp.is_symlink():
        raise ValueError('Symlink manifest')
    manifest=validate_manifest(json.loads(mp.read_text()))
    expected={'manifest.json'}
    for entry in manifest['entries']:
        rel=relative(entry['path']);p=source/rel;expected.add(str(rel))
        if not p.parent.resolve().is_relative_to(source):
            raise ValueError('Snapshot parent escapes through a symlink')
        if entry['kind']=='file':
            if p.is_symlink() or not p.is_file() or p.stat().st_size!=entry['size'] or digest(p)!=entry['sha256']:
                raise ValueError(f'Snapshot file checksum mismatch or missing: {rel}')
        elif not p.is_symlink() or os.readlink(p)!=entry['target'] or not p.resolve().is_relative_to(source) or not p.is_file():
            raise ValueError(f'Invalid snapshot symlink: {rel}')
    actual={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_symlink() or p.is_file()}
    if actual!=expected:
        raise ValueError('Snapshot has extra or missing files')
    if json.loads((source/'app.json').read_text())['app_id']!=manifest['app_id']:
        raise ValueError('Snapshot app identity mismatch')
    with connect(source/'catalog.sqlite3',readonly=True) as db:
        check_database(db)
        check_paths(db,source)
        if counts(db)!=manifest['counts']:
            raise ValueError('Restored catalog counts mismatch')
    return manifest


def create_snapshot(lib, app, dry_run=False):
    appdir=lib.app_dir(app)
    if dry_run:
        with connect(appdir/'catalog.sqlite3',readonly=True) as db:
            check_database(db);check_paths(db,appdir);screen_catalog(db)
            return {'dry_run':True,'app_id':app,'counts':counts(db)}
    with lib.writer(app):
        container=appdir/'snapshots'
        container.mkdir(exist_ok=True)
        if container.is_symlink():
            raise ValueError('Symlink snapshot directory')
        stage=container/('.building-'+uuid.uuid4().hex)
        stage.mkdir()
        backup_database(appdir/'catalog.sqlite3',stage/'catalog.sqlite3')
        shutil.copystat(appdir/'catalog.sqlite3',stage/'catalog.sqlite3')
        shutil.copy2(appdir/'app.json',stage/'app.json')
        # Catalogs are included only through the SQLite backup API. The preserved
        # legacy source DB was itself made with that API at import time.
        original='legacy/ugc/catalog.sqlite3'
        entries,excluded=inventory(appdir/'files',skip=(original,))
        if excluded:
            raise ValueError('Unsafe files found in app storage; inspect locally before snapshot')
        copy_entries(appdir/'files',stage/'files',entries)
        preserved=appdir/'files'/original
        if preserved.exists():
            target=stage/'files'/original
            target.parent.mkdir(parents=True,exist_ok=True)
            backup_database(preserved,target)
            shutil.copystat(preserved,target)
            entries.append(file_entry(target,original))
        entries=[dict(e,path='files/'+e['path']) for e in entries]
        entries += [file_entry(stage/'app.json','app.json'),file_entry(stage/'catalog.sqlite3','catalog.sqlite3')]
        with connect(stage/'catalog.sqlite3',readonly=True) as db:
            check_database(db);check_paths(db,stage);screen_catalog(db)
            payload={'format_version':1,'app_id':app,'counts':counts(db),'entries':sorted(entries,key=lambda x:x['path'])}
        report=appdir/'import-report.json'
        if report.exists():
            import_report=json.loads(report.read_text())
        else:
            imports=lib.rows(app,'vault_imports')
            import_report=json.loads(imports[0]['report_json']) if imports else {}
        payload['excluded']=import_report.get('excluded',[])
        payload['sanitized']=import_report.get('sanitized',[])
        ident=hashlib.sha256(canonical(payload)).hexdigest()
        manifest=dict(payload,snapshot_id=ident)
        write_json(stage/'manifest.json',manifest)
        verify_snapshot(stage)
        destination=container/ident
        if destination.exists():
            verify_snapshot(destination)
            # Leave the redundant staging snapshot for explicit operator cleanup.
            return {'snapshot_id':ident,'path':str(destination),'already_exists':True}
        os.rename(stage,destination)
        # Immutable by CLI policy and read-only permissions; no object-lock claim.
        for p in destination.rglob('*'):
            if not p.is_symlink():
                p.chmod(0o500 if p.is_dir() else p.stat().st_mode & ~0o222)
        destination.chmod(0o500)
        return {'snapshot_id':ident,'path':str(destination),'counts':payload['counts'],'excluded':payload['excluded']}


def export_snapshot(lib,app,ident,destination,dry_run=False):
    source=snapshot_path(lib,app,ident)
    manifest=verify_snapshot(source)
    if manifest['app_id']!=app:
        raise ValueError('App identity mismatch')
    destination=Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Export destination must not exist')
    if dry_run:
        return {'dry_run':True,'snapshot_id':ident,'destination':str(destination)}
    destination.mkdir(parents=True)
    copy_entries(source,destination,manifest['entries'])
    shutil.copyfile(source/'manifest.json',destination/'manifest.json')
    verify_snapshot(destination)
    return {'snapshot_id':ident,'destination':str(destination),'verified':True}


def restore(lib,source,dry_run=False):
    source=Path(source).expanduser().resolve()
    manifest=verify_snapshot(source)
    app=manifest['app_id'];destination=lib.root/'apps'/app
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Restore requires a fresh app directory; existing work is never replaced')
    if dry_run:
        return {'dry_run':True,'app_id':app,'counts':manifest['counts']}
    lib.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock(lib.root/'.registry.lock'):
        parent=lib.root/'apps'
        parent.mkdir(exist_ok=True)
        if parent.is_symlink():
            raise ValueError('Symlink apps directory')
        stage=parent/('.restore-'+uuid.uuid4().hex)
        stage.mkdir()
        copy_entries(source,stage,manifest['entries'])
        (stage/'files').mkdir(exist_ok=True)
        with connect(stage/'catalog.sqlite3',readonly=True) as db:
            check_database(db);check_paths(db,stage)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Another registration created the target app')
        os.rename(stage,destination)
    return {'app_id':app,'snapshot_id':manifest['snapshot_id'],'verified':True,'counts':manifest['counts']}
