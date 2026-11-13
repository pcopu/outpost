"""Upload, independently verify, then evict explicitly selected local payloads.

Archives retain the existing self-contained snapshot format. Missing local
payloads can be streamed from earlier verified archives when making a new one;
no complete restore tree or extra media copy is required on the Mac.
"""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import uuid

from .archive import file_entry, validate_manifest
from .fetch import _metadata, _transport, _chunk_blocks
from .library import PATH_COLUMNS, counts, screen_catalog
from .packed import upload_payload
from .storage import ensure_object, prefix, validate_completion_marker, verify_remote
from .util import (canonical, check_database, connect, digest, excluded_name, inspect_file, inventory, relative,
                   snapshot_database, write_json)


def _real_path(path):
    """Reject every symlink component, including parents of selected sources."""
    path = Path(path).expanduser().absolute()
    if '..' in path.parts or path.resolve() != path:
        raise ValueError('Offload paths must not contain symlinks or traversal')
    return path


def _fingerprint(path):
    s = path.lstat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _manifests(appdir):
    result = {}
    container = appdir / 'snapshots'
    _real_path(container)
    for directory in sorted(container.glob('*'), key=lambda p: p.stat().st_mtime_ns):
        _real_path(directory)
        mp = directory / 'manifest.json'
        if not mp.exists():
            continue  # An unfinished stage is never silently treated as a backup.
        _real_path(mp)
        manifest = validate_manifest(json.loads(mp.read_text()))
        if manifest['app_id'] != appdir.name:
            raise ValueError('Snapshot belongs to another app')
        ident = manifest['snapshot_id']
        if directory.name != ident and not directory.name.startswith('.building-'):
            raise ValueError('Snapshot directory identity mismatch')
        result[ident] = (mp, manifest)
    recovery = _recovery(appdir)
    if recovery.get('snapshot_id') in result:
        ident = recovery['snapshot_id']
        result[ident] = result.pop(ident)
    return result


def _recovery(appdir):
    path = appdir / 'offload-recovery.json'
    _real_path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get('format_version') != 1 or data.get('app_id') != appdir.name:
        raise ValueError('Recovery record identity mismatch')
    return data


def _remote_metadata(store, app, ident):
    base = prefix(app, ident)
    stats = {'remote_metadata_bytes': 0}
    try:
        raw = _metadata(store, base + '/COMMITTED.json', stats, 65536)
    except FileNotFoundError:
        return None
    marker = validate_completion_marker(json.loads(raw), app, ident)
    raw = _metadata(store, base + '/manifest.json', stats)
    if hashlib.sha256(raw).hexdigest() != marker['manifest_sha256']:
        raise ValueError('Remote manifest checksum mismatch')
    manifest = validate_manifest(json.loads(raw))
    if manifest['app_id'] != app or manifest['snapshot_id'] != ident:
        raise ValueError('Remote snapshot identity mismatch')
    if marker.get('transport') != 'packed-v1':
        raise ValueError('Offload requires a packed, verifiable snapshot backend')
    td = _metadata(store, base + '/transport.json', stats)
    if hashlib.sha256(td).hexdigest() != marker['transport_sha256']:
        raise ValueError('Remote transport checksum mismatch')
    descriptor = json.loads(td)
    version, size = _transport(descriptor, manifest)
    return manifest, descriptor, version, size


class _Blocks(io.RawIOBase):
    def __init__(self, blocks):
        self.blocks = iter(blocks)
        self.pending = memoryview(b'')

    def readable(self):
        return True

    def readinto(self, target):
        if not self.pending:
            self.pending = memoryview(next(self.blocks, b''))
        n = min(len(target), len(self.pending))
        target[:n] = self.pending[:n]
        self.pending = self.pending[n:]
        return n

    def close(self):
        if hasattr(self.blocks, 'close'):
            self.blocks.close()
        super().close()


class _Sources:
    def __init__(self, store, app):
        self.store, self.app = store, app
        self.local, self.remote = {}, {}
        self.metadata = {}
        self.cached_key, self.cached_chunk = None, None

    def add_local(self, path, entry):
        if entry['kind'] == 'file' and path.exists():
            _real_path(path)
            self.local.setdefault((entry['sha256'], entry['size']), []).append(path)

    def add_remote(self, ident, meta):
        self.metadata[ident] = meta
        for entry in meta[0]['entries']:
            if entry['kind'] == 'file':
                self.remote.setdefault((entry['sha256'], entry['size']), []).append((ident, entry['path']))

    def blocks(self, ident, rel):
        manifest, descriptor, version, size = self.metadata[ident]
        spec = next(e for e in descriptor['files'] if e['path'] == rel)
        offset, remaining = spec['offset'], spec['size']
        while remaining:
            index = offset // size
            key = (ident, index)
            if self.cached_key != key:
                self.cached_chunk = None
                chunk = descriptor['chunks'][index]
                self.cached_chunk = b''.join(_chunk_blocks(
                    self.store, prefix(self.app, ident) + '/' + chunk['path'], chunk, version))
                self.cached_key = key
            n = min(remaining, size - offset % size, 1024 * 1024)
            yield self.cached_chunk[offset % size:offset % size + n]
            remaining -= n
            offset += n

    @contextmanager
    def open_entry(self, entry):
        key = (entry['sha256'], entry['size'])
        for path in self.local.get(key, []):
            if path.exists():
                _real_path(path)
                if path.stat().st_size == entry['size'] and digest(path) == entry['sha256']:
                    with path.open('rb') as f:
                        yield f
                    return
        remote = self.remote.get(key)
        if not remote:
            raise ValueError('Required snapshot file is missing locally and unavailable in a committed archive')
        ident, rel = remote[-1]
        with io.BufferedReader(_Blocks(self.blocks(ident, rel))) as f:
            yield f

    def tree(self, manifest):
        sources = self
        entries = {e['path']: e for e in manifest['entries']}

        class Tree:
            def __truediv__(self, rel):
                class File:
                    def open(self, mode):
                        if mode != 'rb':
                            raise ValueError('Archive source is read-only')
                        return sources.open_entry(entries[rel])
                return File()
        return Tree()


def _upload(store, app, manifest, mp, sources):
    if not getattr(store, 'packed', False):
        raise ValueError('Offload requires a packed backend')
    base = prefix(app, manifest['snapshot_id'])
    with tempfile.TemporaryDirectory(prefix='outpost-offload-upload-') as temp:
        descriptor, number = upload_payload(store, sources.tree(manifest), manifest,
                                            base, temp, ensure_object)
        ensure_object(store, base + '/manifest.json', mp, temp)
        commit = Path(temp) / 'COMMITTED.json'
        write_json(commit, {'format_version': 1, 'app_id': app,
                            'snapshot_id': manifest['snapshot_id'],
                            'manifest_sha256': digest(mp), 'transport': 'packed-v1',
                            'transport_sha256': digest(descriptor)})
        ensure_object(store, base + '/COMMITTED.json', commit, temp)
    return number + 3


def _source_inventory(lib, sources):
    selected, excluded = {}, []
    for source in sources:
        root = _real_path(source)
        if (not root.is_dir() and not root.is_file()) or root.is_relative_to(lib.root) or lib.root.is_relative_to(root):
            raise ValueError('Source directories must be separate from managed library storage')
        if root.is_file():
            reason = excluded_name(root.name) or inspect_file(root, business=True)
            if reason:
                excluded.append({'path': root.name, 'root': str(root.parent), 'reason': reason})
            else:
                selected[root] = file_entry(root, root.name)
            continue
        entries, skipped = inventory(root, business=True)
        excluded.extend(dict(e, root=str(root)) for e in skipped)
        for entry in entries:
            if entry['kind'] != 'file':
                raise ValueError('Selected source files must be regular files, not symlinks')
            path = _real_path(root / entry['path'])
            selected[path] = entry
    return selected, excluded


def _import_sources(lib, app, selected):
    if not selected:
        return
    assets = [dict(path=str(p), original_path=str(p), sha256=e['sha256'],
                   bytes=e['size'], media_kind='other') for p, e in selected.items()]
    manifest = {'format_version': 1, 'app_id': app, 'assets': assets}
    manifest['bundle_id'] = 'offload-' + hashlib.sha256(canonical(manifest)).hexdigest()
    with tempfile.TemporaryDirectory(prefix='outpost-offload-import-') as temp:
        root = Path(temp).resolve()
        for name in ('entities', 'records', 'metrics', 'relationships', 'asset_links'):
            manifest[name] = name + '.jsonl'
            (root / manifest[name]).write_bytes(b'')
        write_json(root / 'bundle.json', manifest)
        lib.import_bundle(app, root, reference_roots=sorted({p.parent for p in selected}))


def _current_manifest(lib, app, known, stage):
    appdir = lib.app_dir(app)
    entries, holds = {}, {}
    for _, manifest in known.values():
        for entry in manifest['entries']:
            if entry['path'].startswith('files/'):
                entries[entry['path']] = entry
        holds.update({h['path']: h for h in manifest.get('held', [])})
    marker = appdir / 'snapshot-holds.json'
    if marker.exists():
        _real_path(marker)
        holds.update({h['path']: h for h in json.loads(marker.read_text())})
    legacy = 'legacy/ugc/catalog.sqlite3'
    aliases = []
    for rel, entry in entries.items():
        if entry['kind'] == 'symlink':
            path = appdir / rel
            _real_path(path.parent)
            if path.is_symlink() and os.readlink(path) == entry['target']:
                aliases.append(rel[6:])
    present, excluded = inventory(appdir / 'files',
                                  skip=(legacy,) + tuple(p[6:] for p in holds) + tuple(aliases), business=True)
    if excluded:
        raise ValueError('Unreviewed files in managed storage; inspect exclusions before offload')
    for entry in present:
        entry = dict(entry, path='files/' + entry['path'])
        entries[entry['path']] = entry
    legacy_path = appdir / 'files' / legacy
    if legacy_path.exists() and 'files/' + legacy not in holds:
        entries['files/' + legacy] = file_entry(legacy_path, 'files/' + legacy)
    for rel in holds:
        entries.pop(rel, None)
    for rel, entry in entries.items():
        if entry['kind'] == 'symlink':
            target = os.path.normpath(str(Path(rel).parent / entry['target']))
            if target not in entries or entries[target]['kind'] != 'file':
                raise ValueError('Snapshot alias target is missing or unsafe')
    with connect(appdir / 'catalog.sqlite3', readonly=True) as db:
        check_database(db)
        screen_catalog(db)
        for table, columns in dict(PATH_COLUMNS, vault_assets=('stored_path',)).items():
            for col in columns:
                for (value,) in db.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL AND "{col}" != \'\''):
                    relative(value)
                    entry = entries.get(value) or holds.get(value)
                    if not entry:
                        raise ValueError('Catalog references a missing file without an archive mapping')
                    if table == 'vault_assets':
                        expected = db.execute('SELECT sha256 FROM vault_assets WHERE stored_path=?', (value,)).fetchone()[0]
                        if entry.get('sha256') != expected:
                            raise ValueError('Archive mapping differs from current catalog asset hash')
        catalog_counts = counts(db)
    snapshot_database(appdir / 'catalog.sqlite3', stage / 'catalog.sqlite3')
    # Match original timestamps so unchanged offloads reuse the same snapshot ID.
    shutil.copystat(appdir / 'catalog.sqlite3', stage / 'catalog.sqlite3')
    entries['catalog.sqlite3'] = file_entry(stage / 'catalog.sqlite3', 'catalog.sqlite3')
    entries['app.json'] = file_entry(appdir / 'app.json', 'app.json')
    previous = next(reversed(known.values()))[1] if known else {}
    payload = {'format_version': 1, 'app_id': app, 'entries': sorted(entries.values(), key=lambda e:e['path']),
               'counts': catalog_counts, 'excluded': previous.get('excluded', []),
               'sanitized': previous.get('sanitized', [])}
    if holds:
        payload['held'] = sorted(holds.values(), key=lambda e:e['path'])
    ident = hashlib.sha256(canonical(payload)).hexdigest()
    return validate_manifest(dict(payload, snapshot_id=ident))


def _candidate(path, entry, candidates):
    if not path.exists() and not path.is_symlink():
        return
    if entry['kind'] != 'file':
        return  # Tiny aliases remain; never follow them to delete their targets.
    _real_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Offload candidate must be a regular file')
    if path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
        raise ValueError('Local file changed since archive selection; no deletion performed')
    candidates[path] = dict(entry, fingerprint=_fingerprint(path))


def _write_recovery(appdir, data):
    target = appdir / 'offload-recovery.json'
    _real_path(target)
    temp = appdir / ('.offload-recovery-' + uuid.uuid4().hex)
    write_json(temp, data)
    os.replace(temp, target)
    fd = os.open(appdir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_checked(path, entry):
    # Open each parent without following symlinks. A concurrent rename must
    # never redirect deletion outside the selected directory tree.
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        s = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        fingerprint = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if fingerprint != entry['fingerprint'] or not stat.S_ISREG(s.st_mode):
            raise ValueError('Local file changed before deletion; stopped')
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if not mode & stat.S_IWUSR:
            os.fchmod(fd, mode | stat.S_IWUSR)
        try:
            os.unlink(path.name, dir_fd=fd)
        finally:
            if not mode & stat.S_IWUSR:
                os.fchmod(fd, mode)
    finally:
        os.close(fd)


def offload(lib, app, store, sources=(), dry_run=False, progress=None, copies=()):
    """Move app payloads and explicitly selected source directories to storage.

    Every archive is verified in full during this invocation before the first
    deletion. The live database/identity, manifests, holds and recovery map stay.
    """
    appdir = lib.app_dir(app)
    copy_roots = [_real_path(p) for p in copies]
    for root in copy_roots:
        if (not root.is_file() and not root.is_dir()) or root.is_relative_to(lib.root) or lib.root.is_relative_to(root):
            raise ValueError('Staging copies must be separate from the managed library')
    selected, excluded = _source_inventory(lib, sources)
    known = _manifests(appdir)
    report = {'app_id': app, 'dry_run': dry_run, 'excluded': excluded,
              'selected_source_files': len(selected),
              'selected_source_bytes': sum(e['size'] for e in selected.values()),
              'selected_copy_paths': [str(p) for p in copy_roots],
              'existing_snapshots': list(known), 'verified': False}
    if dry_run:
        report['action'] = 'upload_missing_verify_all_then_remove_local_payloads'
        return report
    if not getattr(store, 'packed', False):
        raise ValueError('Offload requires a packed backend')
    if hasattr(store, 'root'):
        remote_root = Path(store.root).resolve()
        roots = [lib.root] + [_real_path(p) for p in sources] + copy_roots
        if any(remote_root.is_relative_to(root) or root.is_relative_to(remote_root) for root in roots):
            raise ValueError('Archive destination must be separate from the live library')
    notify = progress or (lambda message: None)
    _import_sources(lib, app, selected)
    free_before = shutil.disk_usage(appdir).free
    with lib.writer(app), tempfile.TemporaryDirectory(prefix='outpost-offload-') as temp:
        stage = Path(temp).resolve()
        known = _manifests(appdir)
        current = _current_manifest(lib, app, known, stage)
        ident = current['snapshot_id']
        mp = stage / 'manifest.json'
        write_json(mp, current)
        if ident in known:
            if digest(known[ident][0]) != digest(mp):
                raise ValueError('Existing snapshot manifest bytes conflict')
        else:
            known[ident] = (mp, current)
        provider = _Sources(store, app)
        for local_mp, manifest in known.values():
            for entry in manifest['entries']:
                provider.add_local(local_mp.parent / entry['path'], entry)
        for entry in current['entries']:
            provider.add_local(appdir / entry['path'], entry)
        provider.add_local(stage / 'catalog.sqlite3',
                           next(e for e in current['entries'] if e['path']=='catalog.sqlite3'))
        for snapshot in known:
            meta = _remote_metadata(store, app, snapshot)
            if meta is not None:
                provider.add_remote(snapshot, meta)
        proofs = []
        for snapshot, (local_mp, manifest) in known.items():
            if snapshot not in provider.metadata:
                notify('Uploading missing archive ' + app + '/' + snapshot)
                _upload(store, app, manifest, local_mp, provider)
                provider.add_remote(snapshot, _remote_metadata(store, app, snapshot))
            notify('Verifying every remote file in ' + app + '/' + snapshot)
            proofs.append(verify_remote(store, app, snapshot, local_mp))
        candidates = {}
        for local_mp, manifest in known.values():
            if local_mp.parent == stage:
                continue
            for entry in manifest['entries']:
                _candidate(local_mp.parent / entry['path'], entry, candidates)
        for entry in current['entries']:
            if entry['path'].startswith('files/'):
                _candidate(appdir / entry['path'], entry, candidates)
        verified_entries = {(e['sha256'], e['size']): (snapshot, e)
                            for snapshot, (_, m) in known.items()
                            for e in m['entries'] if e['kind']=='file'}
        retained_staging, staging_copies = [], []
        for directory in (appdir / 'snapshots').glob('.building-*'):
            _real_path(directory)
            for path in directory.rglob('*'):
                if path.name == 'manifest.json' or path in candidates or path.is_dir():
                    continue
                if path.is_symlink():
                    retained_staging.append(str(path))
                    continue
                _real_path(path)
                match = verified_entries.get((digest(path), path.stat().st_size))
                if match:
                    snapshot, entry = match
                    _candidate(path, entry, candidates)
                    staging_copies.append({'path': str(path), 'snapshot_id': snapshot,
                                           'stored_path': entry['path'], 'sha256': entry['sha256'],
                                           'size': entry['size']})
                else:
                    retained_staging.append(str(path))
        for path, entry in selected.items():
            if (entry['sha256'], entry['size']) not in verified_entries:
                raise ValueError('Selected source is missing from verified archives')
            _candidate(path, entry, candidates)
        retained_copies = []
        for root in copy_roots:
            for path in ([root] if root.is_file() else root.rglob('*')):
                if path in candidates:
                    continue
                if path.is_symlink():
                    retained_copies.append(str(path))
                    continue
                if path.is_dir():
                    continue
                _real_path(path)
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError('Staging copy must be a regular file')
                match = verified_entries.get((digest(path), path.stat().st_size))
                if match:
                    snapshot, entry = match
                    _candidate(path, entry, candidates)
                    staging_copies.append({'path': str(path), 'snapshot_id': snapshot,
                                           'stored_path': entry['path'], 'sha256': entry['sha256'],
                                           'size': entry['size']})
                else:
                    retained_copies.append(str(path))
        # Check the complete set before touching any payload, including paths
        # altered by external programs while upload/read-back was in progress.
        for path, entry in candidates.items():
            _real_path(path)
            if _fingerprint(path) != entry['fingerprint'] or digest(path) != entry['sha256']:
                raise ValueError('Local file changed before deletion; no deletion performed')
        destination = appdir / 'snapshots' / ident
        _real_path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        retained = destination / 'manifest.json'
        if retained.exists():
            if digest(retained) != digest(mp):
                raise ValueError('Conflicting retained recovery manifest')
        else:
            write_json(retained, current)
        source_files = {(e['path'], e['sha256']): e for e in _recovery(appdir).get('source_files', [])}
        for p, e in selected.items():
            source_files[(str(p), e['sha256'])] = {'path': str(p), 'sha256': e['sha256'], 'size': e['size'],
                                                'stored_path': 'files/assets/' + e['sha256']}
        recovery = {'format_version': 1, 'app_id': app, 'snapshot_id': ident,
                    'verified_at': time.time(), 'proofs': proofs,
                    'source_files': list(source_files.values()),
                    'staging_copies': _recovery(appdir).get('staging_copies', []) + staging_copies,
                    'retained_unmatched_snapshot_files': retained_staging,
                    'retained_unmatched_copies': retained_copies,
                    'planned_deletions': len(candidates), 'deleted_files': 0,
                    'logical_bytes_removed': 0, 'state': 'verified_before_deletion'}
        _write_recovery(appdir, recovery)
        notify('Cloud verification passed; removing ' + str(len(candidates)) + ' covered local files')
        for path, entry in candidates.items():
            _unlink_checked(path, entry)
            recovery['deleted_files'] += 1
            recovery['logical_bytes_removed'] += entry['size']
        recovery.update(state='complete', free_space_change_bytes=shutil.disk_usage(appdir).free-free_before)
        _write_recovery(appdir, recovery)
    return dict(report, snapshot_id=ident, verified=True, deleted_files=recovery['deleted_files'],
                logical_bytes_removed=recovery['logical_bytes_removed'],
                free_space_change_bytes=recovery['free_space_change_bytes'],
                retained_unmatched_snapshot_files=retained_staging,
                retained_unmatched_copies=retained_copies,
                held_files=len(current.get('held', [])), recovery_file=str(appdir/'offload-recovery.json'))
