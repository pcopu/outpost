"""Verified, restartable SQLite-to-D1 migration with a small local reference."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .cloud import CloudRequestError, encode_row
from .offload import _fingerprint, _real_path, _unlink_checked
from .util import canonical, connect, digest, write_json


def quoted(name):
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', name):
        raise ValueError('Unsupported catalog identifier')
    return '"' + name + '"'


def schema(db):
    result = []
    rows = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
    for row in rows:
        entry = dict(row); quoted(entry['name'])
        if entry['type'] not in ('table', 'index', 'view') or 'WITHOUT ROWID' in entry['sql'].upper() or 'VIRTUAL TABLE' in entry['sql'].upper():
            raise ValueError('Catalog schema requires a separately reviewed migration')
        if entry['type'] in ('table', 'view'):
            entry['columns'] = [dict(r) for r in db.execute('PRAGMA table_info(' + quoted(entry['name']) + ')')]
            if len(entry['columns']) > 99 or any(c['name'].lower() in ('rowid', '_rowid_', 'oid', '__outpost_rowid__') for c in entry['columns']):
                raise ValueError('Catalog has unsupported row identity or column count')
        if entry['type'] == 'table':
            entry['foreign_keys'] = [dict(r) for r in db.execute('PRAGMA foreign_key_list(' + quoted(entry['name']) + ')')]
        result.append(entry)
    return result


def ordered_tables(entries):
    remaining = {s['name']: s for s in entries if s['type'] == 'table'}
    done, result = set(), []
    while remaining:
        ready = [name for name, s in remaining.items()
                 if {f['table'] for f in s['foreign_keys'] if f['table'] != name} <= done]
        if not ready:
            raise ValueError('Cross-table foreign-key cycle requires a reviewed migration')
        for name in sorted(ready):
            result.append(remaining.pop(name)); done.add(name)
    return result


def pages(db, entry, put_blob, *, self_links=False):
    names = [c['name'] for c in entry['columns']]
    links = {f['from'] for f in entry['foreign_keys'] if f['table'] == entry['name']}
    if self_links:
        names = [name for name in names if name in links]
        if not names:
            return
    columns = ','.join(quoted(name) for name in names)
    sql = 'SELECT rowid AS __outpost_rowid__,' + columns + ' FROM ' + quoted(entry['name']) + ' ORDER BY rowid'
    page, size = [], 0
    for row in db.execute(sql):
        encoded = encode_row({k: row[k] for k in names}, put_blob)
        value = [row['__outpost_rowid__']] + [encoded[k] for k in names]
        count = len(canonical(value))
        if page and (len(page) >= 250 or size + count > 1500000):
            yield page; page, size = [], 0
        page.append(value); size += count
    if page:
        yield page


def atomic_json(path, data):
    _real_path(path)
    temporary = path.parent / ('.cloud-' + uuid.uuid4().hex)
    try:
        write_json(temporary, data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(lib, app, remote, dry_run=False, progress=None):
    notify = progress or (lambda text: None)
    appdir = lib.app_dir(app); source = _real_path(appdir / 'catalog.sqlite3')
    if remote.reference['app_id'] != app:
        raise ValueError('Migration target belongs to another app')
    if (appdir / 'cloud.json').exists() and not source.exists():
        return {'app_id': app, 'already_migrated': True, 'dry_run': dry_run,
                'cloud': None if dry_run else remote.request('state')}
    if not source.is_file():
        raise ValueError('Local catalog is missing')
    recovery_path = appdir / 'offload-recovery.json'
    if not recovery_path.exists():
        raise ValueError('A fully verified catalog backup is required before migration; run offload first')
    with lib.writer(app):
        before = _fingerprint(source); source_sha = digest(source)
        recovery = json.loads(recovery_path.read_text())
        proofs = [p for p in recovery.get('proofs', []) if p.get('verified') and p.get('catalog_sha256') == source_sha and p.get('app_id') == app]
        if recovery.get('state') != 'complete' or not proofs:
            raise ValueError('Current catalog has no matching verified backup; run offload before migration')
        backup = proofs[-1]
        free_before = shutil.disk_usage(appdir).free
        with connect(source, readonly=True) as db:
            db.execute('BEGIN')
            entries = schema(db); tables = ordered_tables(entries)
            has_sequence = db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
            sequences = dict(db.execute('SELECT name,seq FROM sqlite_sequence')) if has_sequence else {}
            counts = {s['name']: db.execute('SELECT COUNT(*) FROM ' + quoted(s['name'])).fetchone()[0] for s in tables}
            for s in tables:
                self_columns = {f['from'] for f in s['foreign_keys'] if f['table'] == s['name']}
                if any(c['notnull'] for c in s['columns'] if c['name'] in self_columns):
                    raise ValueError('Nonnullable self-reference requires a reviewed migration')
            report = {'app_id': app, 'dry_run': dry_run, 'source_sha256': source_sha,
                      'source_bytes': source.stat().st_size, 'counts': counts,
                      'rows': sum(counts.values()), 'backup': backup,
                      'database_id': remote.reference['database_id']}
            if dry_run:
                return report
            notify(f'{app}: initializing private cloud catalog; {report["rows"]} rows')
            initialized = remote.request('init', {'app_id': app, 'source_sha256': source_sha, 'schema': entries, 'sequences': sequences, 'backup': backup})
            # Tables are topologically ordered. Nullable self-links are restored
            # only after every row in that table exists, preserving FK checks.
            # Activation can succeed immediately before an interrupted local
            # removal. Reverify that catalog rather than attempting new imports.
            for entry in ([] if initialized['phase'] == 'ready' else tables):
                name = entry['name']; names = [c['name'] for c in entry['columns']]
                links = [c for c in names if any(f['from'] == c and f['table'] == name for f in entry['foreign_keys'])]
                total = 0
                with ThreadPoolExecutor(max_workers=3) as pool:
                    pending = []
                    for number, page in enumerate(pages(db, entry, remote.upload_blob)):
                        selected = [r.copy() for r in page]
                        for row in selected:
                            for column in links: row[names.index(column) + 1] = None
                        data = {'table': name, 'batch_id': name + ':insert:' + str(number), 'columns': names, 'rows': selected, 'mode': 'insert'}
                        pending.append(pool.submit(remote.request, 'import', data)); total += len(page)
                        if len(pending) >= 3:
                            pending.pop(0).result()
                        if number and number % 100 == 0:
                            notify(f'{app}: imported {total}/{counts[name]} rows in {name}')
                    for future in pending: future.result()
                if links:
                    for number, page in enumerate(pages(db, entry, remote.upload_blob, self_links=True)):
                        remote.request('import', {'table': name, 'batch_id': name + ':links:' + str(number), 'columns': links, 'rows': page, 'mode': 'self-links'})
                if total: notify(f'{app}: imported {total} rows in {name}')
            # A separate full read-back compares every stored scalar and row ID.
            # Oversized values were independently SHA-256 checked in upload_blob;
            # their content hashes and byte lengths participate in this proof.
            table_hashes = {}
            for entry in tables:
                name = entry['name']; h = hashlib.sha256(); verified = 0
                with ThreadPoolExecutor(max_workers=3) as pool:
                    pending = []
                    def check(future, expected):
                        actual = future.result()['rows']
                        if actual != expected:
                            raise ValueError('Cloud row verification failed; local catalog retained')
                        return len(expected)
                    for number, page in enumerate(pages(db, entry, remote.upload_blob)):
                        h.update(canonical(page)); h.update(b'\n')
                        future = pool.submit(remote.request, 'scan', {'table': name, 'start': page[0][0], 'end': page[-1][0]})
                        pending.append((future, page))
                        if len(pending) >= 3: verified += check(*pending.pop(0))
                        if number and number % 100 == 0: notify(f'{app}: verified {verified}/{counts[name]} rows in {name}')
                    for item in pending: verified += check(*item)
                if verified != counts[name]:
                    raise ValueError('Cloud row-count verification failed; local catalog retained')
                table_hashes[name] = h.hexdigest()
                if verified: notify(f'{app}: verified {verified} rows in {name}')
            verification = hashlib.sha256(canonical({'source_sha256': source_sha, 'counts': counts, 'table_hashes': table_hashes, 'sequences': sequences})).hexdigest()
            # Activation is idempotent by verification hash. A rejected D1
            # transaction can be retried safely; persistent conflicts still
            # fail before publishing the reference or removing local SQLite.
            for attempt in range(3):
                try:
                    remote.request('finalize', {'counts': counts, 'verification_sha256': verification})
                    break
                except CloudRequestError as error:
                    if error.status != 409 or attempt == 2:
                        raise
                    time.sleep(1 + attempt)
        if _fingerprint(source) != before or digest(source) != source_sha:
            raise ValueError('Local catalog changed during migration; local copy retained')
        for suffix in ('-wal', '-shm', '-journal'):
            sibling = Path(str(source) + suffix)
            if sibling.exists() and sibling.stat().st_size:
                raise ValueError('Local catalog has active SQLite sidecars; local copy retained')
        report.update(verified=True, verification_sha256=verification, table_hashes=table_hashes,
                      verified_at=time.time(), full_sqlite_restore_performed=False,
                      state='verified_before_local_removal')
        receipt = appdir / 'cloud-migration.json'
        atomic_json(receipt, report)
        reference = dict(remote.reference, original_catalog_sha256=source_sha,
                         original_snapshot_id=backup['snapshot_id'], verification_sha256=verification)
        atomic_json(appdir / 'cloud.json', reference)
        _unlink_checked(source, {'fingerprint': before})
        report.update(state='complete', local_database_removed=True,
                      free_space_change_bytes=shutil.disk_usage(appdir).free-free_before)
        atomic_json(receipt, report)
        notify(f'{app}: cloud catalog active; removed {report["source_bytes"]} local database bytes')
        return report
