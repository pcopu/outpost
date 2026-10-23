"""Per-app SQLite and immutable original-file imports."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .util import (backup_database, canonical, check_database, connect, copy_entries,
                   digest, excluded_name, inspect_file, inventory, local_filesystem,
                   lock, relative, sanitize_json, secret_data, slug, write_json)


DOMAIN_TABLES = ('accounts','videos','comments','video_metric_snapshots','ad_usage',
                 'ad_metric_snapshots','local_assets','local_asset_sources',
                 'local_asset_relations','local_asset_ad_usage',
                 'local_asset_ad_metric_snapshots','sync_runs')
VAULT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS vault_assets (
 asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, stored_path TEXT NOT NULL,
 file_bytes INTEGER NOT NULL, media_kind TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS vault_asset_sources (
 source_path TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES vault_assets(sha256),
 PRIMARY KEY(source_path,sha256));
CREATE TABLE IF NOT EXISTS vault_path_provenance (
 table_name TEXT NOT NULL, record_key TEXT NOT NULL, column_name TEXT NOT NULL,
 original_path TEXT NOT NULL, stored_path TEXT,
 PRIMARY KEY(table_name,record_key,column_name));
CREATE TABLE IF NOT EXISTS vault_imports (
 import_id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL, report_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_file_provenance (
 stored_path TEXT PRIMARY KEY, source_sha256 TEXT, stored_sha256 TEXT,
 action TEXT NOT NULL CHECK(action IN ('copied','redacted')));
'''
PATH_COLUMNS = {'videos':('video_path','audio_path','subtitle_path','thumbnail_path','raw_metadata_path'),
                'local_assets':('canonical_path','audio_path'), 'local_asset_sources':('source_path',)}
READ_TABLES = DOMAIN_TABLES + ('catalog_meta','vault_assets','vault_asset_sources','vault_path_provenance','vault_imports','vault_file_provenance')


def initialize(path):
    c = sqlite3.connect(path)
    try:
        c.executescript(Path(__file__).with_name('schema.sql').read_text())
        c.executescript(VAULT_SCHEMA)
        c.execute("INSERT OR IGNORE INTO catalog_meta VALUES ('schema_version','2')")
        c.execute("INSERT OR IGNORE INTO catalog_meta VALUES ('outpost_schema_version','1')")
        c.commit()
    finally:
        c.close()


def counts(db):
    return {t:db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in DOMAIN_TABLES}


def screen_catalog(db):
    for table in DOMAIN_TABLES:
        for row in db.execute(f'SELECT * FROM "{table}"'):
            if secret_data(dict(row)):
                raise ValueError(f'Possible credential in catalog table {table}; review locally before import/upload')


class Library:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        local_filesystem(self.root)

    def app_dir(self, app):
        slug(app)
        path = self.root / 'apps' / app
        for p in (self.root/'apps', path):
            if p.is_symlink():
                raise ValueError('Symlink application directory is not allowed')
        if not (path/'app.json').is_file() or (path/'app.json').is_symlink():
            raise ValueError(f'Unknown app: {app}')
        meta = json.loads((path/'app.json').read_text())
        if meta['app_id'] != app:
            raise ValueError('App identity mismatch')
        if (path/'catalog.sqlite3').is_symlink():
            raise ValueError('Symlink catalog is not allowed')
        if (path/'files').is_symlink():
            raise ValueError('Symlink file storage is not allowed')
        return path

    @contextmanager
    def writer(self, app):
        path = self.app_dir(app)
        with lock(path/'.writer.lock'):
            yield path

    def register(self, app, name, dry_run=False):
        slug(app)
        if not name or secret_data(name):
            raise ValueError('Provide an app name without credentials')
        meta = {'app_id':app, 'name':name, 'format_version':1}
        target = self.root/'apps'/app
        if target.exists() or target.is_symlink():
            existing = json.loads((self.app_dir(app)/'app.json').read_text())
            if existing != meta:
                raise ValueError('App ID already registered with different metadata')
            return existing
        if dry_run:
            return dict(meta,dry_run=True)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock(self.root/'.registry.lock'):
            (self.root/'apps').mkdir(exist_ok=True)
            if (self.root/'apps').is_symlink():
                raise ValueError('Symlink apps directory')
            target.mkdir()
            write_json(target/'app.json',meta)
            initialize(target/'catalog.sqlite3')
            (target/'files').mkdir()
        return meta

    def apps(self):
        return [json.loads((self.app_dir(p.name)/'app.json').read_text())
                for p in sorted((self.root/'apps').glob('*')) if not p.name.startswith('.')]

    def rows(self, app, table, filters=None, limit=None):
        if table not in READ_TABLES:
            raise ValueError('Unknown table')
        with connect(self.app_dir(app)/'catalog.sqlite3',readonly=True) as db:
            columns = {r['name'] for r in db.execute(f'PRAGMA table_info("{table}")')}
            filters = filters or {}
            if not filters.keys() <= columns:
                raise ValueError('Unknown filter column')
            sql = f'SELECT * FROM "{table}"'
            if filters:
                sql += ' WHERE ' + ' AND '.join(f'"{x}" = ?' for x in filters)
            values = list(filters.values())
            if limit is not None:
                if int(limit) < 1:
                    raise ValueError('Limit must be positive')
                sql += ' LIMIT ?'; values.append(int(limit))
            return [dict(r) for r in db.execute(sql, values)]

    def record(self, app, table, row, dry_run=False):
        if table not in DOMAIN_TABLES or not row or secret_data(row):
            raise ValueError('Invalid record, table, or possible credential')
        path = self.app_dir(app)
        with connect(path/'catalog.sqlite3',readonly=True) as db:
            info = list(db.execute(f'PRAGMA table_info("{table}")'))
            cols = {r['name'] for r in info}
            keys = [r['name'] for r in sorted(info,key=lambda x:x['pk']) if r['pk']]
        if not row.keys() <= cols or not all(k in row for k in keys):
            raise ValueError('Unknown column or missing primary key')
        for k in PATH_COLUMNS.get(table,()):
            if row.get(k):
                file=path/relative(row[k])
                if not file.is_file() or not file.resolve().is_relative_to(path):
                    raise ValueError('Catalog paths must reference existing files inside this app')
        if dry_run:
            return {'dry_run':True,'table':table,'row':row}
        with self.writer(app), connect(path/'catalog.sqlite3') as db:
            names = ','.join(f'"{x}"' for x in row)
            update = ','.join(f'"{x}"=excluded."{x}"' for x in row if x not in keys)
            clause = 'DO UPDATE SET '+update if update else 'DO NOTHING'
            db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in row)}) ON CONFLICT({",".join(keys)}) {clause}',list(row.values()))
        return {'table':table,'primary_key':{k:row[k] for k in keys}}

    def export_catalog(self,app):
        with connect(self.app_dir(app)/'catalog.sqlite3',readonly=True) as db:
            db.execute('BEGIN')
            check_database(db)
            return {'app_id':app,'tables':{table:[dict(row) for row in db.execute(f'SELECT * FROM "{table}"')] for table in READ_TABLES}}

    def import_asset(self, app, source, media_kind='other', metadata=None, dry_run=False):
        source = Path(source).expanduser().absolute()
        path = self.app_dir(app)
        if source.is_symlink() or not source.is_file():
            raise ValueError('Asset must be a regular original file, not a symlink')
        why = excluded_name(source.name) or inspect_file(source)
        if why or secret_data(metadata or {}):
            raise ValueError(why or 'Possible credential in metadata')
        sha = digest(source)
        rel = 'files/assets/'+sha
        result = {'asset_id':sha,'sha256':sha,'stored_path':rel,'dry_run':dry_run}
        if dry_run:
            return result
        with self.writer(app), connect(path/'catalog.sqlite3') as db:
            target = path/rel
            if target.parent.is_symlink() or not target.parent.resolve().is_relative_to(path):
                raise ValueError('Asset destination escapes app storage')
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.is_symlink():
                raise ValueError('Symlink asset destination')
            if target.exists():
                if digest(target) != sha:
                    raise ValueError('Existing asset checksum mismatch')
            else:
                temp = target.with_name('.'+uuid.uuid4().hex+'.partial')
                shutil.copy2(source,temp)
                if digest(temp) != sha:
                    raise ValueError('Source changed during asset import; partial retained')
                os.rename(temp,target)
            db.execute('INSERT OR IGNORE INTO vault_assets VALUES (?,?,?,?,?,?)',(sha,sha,rel,target.stat().st_size,media_kind,json.dumps(metadata or {},sort_keys=True)))
            db.execute('INSERT OR IGNORE INTO vault_asset_sources VALUES (?,?)',(str(source),sha))
        return result

    def import_legacy(self, app, source, dry_run=False):
        source = Path(source).expanduser().resolve()
        path = self.app_dir(app)
        if source == path or path.is_relative_to(source) or source.is_relative_to(path):
            raise ValueError('Source and app storage must be separate')
        dbrel = 'ugc/catalog.sqlite3'
        with tempfile.TemporaryDirectory(prefix='outpost-import-') as scratch:
            backed = Path(scratch)/'source.sqlite3'
            backup_database(source/dbrel,backed)
            with connect(backed,readonly=True) as db:
                screen_catalog(db)
                original_counts = counts(db)
            entries, excluded = inventory(source, skip=(dbrel,dbrel+'-wal',dbrel+'-shm',dbrel+'-journal'))
            overrides={};sanitized=[]
            for item in excluded[:]:
                original=source/item['path']
                if item['reason'].startswith('possible embedded credential') and original.suffix.lower()=='.json' and original.stat().st_size<=32*1024*1024:
                    try:
                        derivative,changes=sanitize_json(json.loads(original.read_text()))
                    except (ValueError,UnicodeError):
                        continue
                    if not changes or secret_data(derivative):
                        continue
                    target=Path(scratch)/'redacted'/item['path']
                    target.parent.mkdir(parents=True,exist_ok=True)
                    write_json(target,derivative)
                    st=original.stat();source_sha=digest(original)
                    entries.append({'path':item['path'],'kind':'file','sha256':digest(target),'size':target.stat().st_size,'mode':st.st_mode & 0o777,'mtime_ns':st.st_mtime_ns,'source_sha256':source_sha})
                    overrides[item['path']]=target
                    sanitized.append({'path':item['path'],'redacted_fields':changes,'source_sha256':source_sha})
                    excluded.remove(item)
            entries.sort(key=lambda e:e['path'])
            fingerprint = hashlib.sha256(canonical({'entries':entries,'db':digest(backed),'excluded':excluded})).hexdigest()
            existing = self.rows(app,'vault_imports')
            if existing:
                if existing[0]['source_fingerprint'] != fingerprint:
                    raise ValueError('Legacy source changed; baseline is immutable. Import new assets separately or register a new app for a new baseline.')
                return dict(json.loads(existing[0]['report_json']),already_imported=True)
            report = {'source_fingerprint':fingerprint,'counts':original_counts,'files':len(entries)+1,'bytes':sum(x.get('size',0) for x in entries)+backed.stat().st_size,'excluded':excluded,'sanitized':sanitized,'dry_run':dry_run}
            if dry_run:
                return report
            with self.writer(app):
                with connect(path/'catalog.sqlite3',readonly=True) as db:
                    if any(counts(db).values()) or db.execute('SELECT count(*) FROM vault_assets').fetchone()[0]:
                        raise ValueError('Legacy baseline requires an empty app catalog')
                staging = path/('.import-'+uuid.uuid4().hex)
                staging.mkdir()
                files = staging/'legacy'
                files.mkdir()
                copy_entries(source,files,entries,overrides)
                (files/'ugc').mkdir(exist_ok=True)
                shutil.copy2(backed,files/dbrel)
                working = staging/'catalog.sqlite3'
                shutil.copy2(backed,working)
                with connect(working) as db:
                    db.executescript(VAULT_SCHEMA)
                    db.execute("INSERT OR IGNORE INTO catalog_meta VALUES ('outpost_schema_version','1')")
                    for entry in entries:
                        if entry['kind']=='file':
                            db.execute('INSERT INTO vault_file_provenance VALUES (?,?,?,?)',('files/legacy/'+entry['path'],entry.get('source_sha256',entry['sha256']),entry['sha256'],'redacted' if entry['path'] in overrides else 'copied'))
                    available = {x['path'] for x in entries}
                    for table, columns in PATH_COLUMNS.items():
                        info = list(db.execute(f'PRAGMA table_info("{table}")'))
                        pk = next(x['name'] for x in info if x['pk'])
                        for row in list(db.execute(f'SELECT * FROM "{table}"')):
                            for col in columns:
                                old = row[col]
                                if not old:
                                    continue
                                original = Path(old)
                                rel = original.relative_to(source).as_posix() if original.is_absolute() and original.is_relative_to(source) else old
                                relative(rel)
                                if rel not in available:
                                    raise ValueError(f'Catalog references missing or excluded marketing file in {table}.{col}; review source locally')
                                mapped = 'files/legacy/'+rel
                                db.execute('INSERT INTO vault_path_provenance VALUES (?,?,?,?,?)',(table,str(row[pk]),col,old,mapped))
                                db.execute(f'UPDATE "{table}" SET "{col}"=? WHERE "{pk}"=?',(mapped,row[pk]))
                    db.execute('INSERT INTO vault_imports VALUES (?,?,?)',('legacy',fingerprint,json.dumps(report,sort_keys=True)))
                    check_database(db)
                destination = path/'files/legacy'
                if destination.exists():
                    raise FileExistsError('Uncommitted legacy files exist; inspect them before retrying')
                os.rename(files,destination)
                # Files become durable before the one atomic database replacement.
                os.replace(working,path/'catalog.sqlite3')
                write_json(path/'import-report.json',report)
                staging.rmdir()
            return report

    def snapshot(self, app, dry_run=False):
        from .archive import create_snapshot
        return create_snapshot(self,app,dry_run)

    def export_snapshot(self, app, snapshot_id, destination, dry_run=False):
        from .archive import export_snapshot
        return export_snapshot(self,app,snapshot_id,destination,dry_run)

    def restore(self, source, dry_run=False):
        from .archive import restore
        return restore(self,source,dry_run)
