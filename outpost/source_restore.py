"""Portable, standard-library-only source restore. Never executes project code.

This module deliberately does not import the POSIX-only catalog/locking modules.
It can also be copied alongside a downloaded manifest and run directly.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_FILES = 2000
MAX_BYTES = 20 * 1024 ** 3
CHUNK = 8 * 1024 ** 2
MANIFEST_LIMIT = 900000


def canonical(value):
    def numbers(item):
        if isinstance(item, float) or type(item) is int and abs(item) > 2 ** 53 - 1:
            raise ValueError('Use strings for decimal or oversized numeric source metadata')
        if isinstance(item, dict):
            if not all(isinstance(key, str) and key.isascii() and not key.isdecimal() for key in item):
                raise ValueError('Source metadata keys must be named ASCII keys for portable canonical hashes')
            for child in item.values(): numbers(child)
        elif isinstance(item, list):
            for child in item: numbers(child)
    numbers(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 800 or unicodedata.normalize('NFC', value) != value:
        raise ValueError('Invalid portable relative path')
    parts = value.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part[-1] in '. ' or
                re.search(r'[\\:<>"|?*\x00-\x1f\x7f]', part) or
                re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\..*)?', part) or
                part.lower() in ('.git', '.env', 'outpost-source.json') or
                part.lower().startswith(('.env.', '.outpost-'))):
            raise ValueError('Unsafe or reserved restore path')
    return value


def validate_paths(paths):
    seen = set()
    for path in paths:
        value = safe_path(path).upper().lower()
        if value in seen or any(value.startswith(p + '/') or p.startswith(value + '/') for p in seen):
            raise ValueError('Case collision or file/directory conflict')
        seen.add(value)


def valid_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid SHA-256')
    return value


def validate(revision):
    if revision.get('format_version') != 1:
        raise ValueError('Unsupported source revision')
    manifest = revision['manifest']
    if not isinstance(revision.get('label'), str) or len(revision['label']) > 300:
        raise ValueError('Source revision label must be at most 300 characters')
    if sha(canonical(manifest)) != valid_hash(revision['revision_id']):
        raise ValueError('Source revision checksum mismatch')
    if len(canonical(revision)) > MANIFEST_LIMIT:
        raise ValueError('Source manifest is too large')
    if not re.fullmatch('[a-z][a-z0-9-]{0,62}', manifest['app_id']) or not isinstance(manifest['video_id'], str) or not 1 <= len(manifest['video_id']) <= 200:
        raise ValueError('Invalid source identity')
    files = manifest['files']
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError('Source file count exceeds limit')
    validate_paths([f['path'] for f in files])
    total = 0
    for file in files:
        valid_hash(file['sha256'])
        if type(file['bytes']) is not int or not 0 <= file['bytes'] <= MAX_BYTES:
            raise ValueError('Invalid source byte count')
        total += file['bytes']
        if file['role'] not in ('source', 'transcript', 'audio', 'raw', 'font', 'dependency', 'archive', 'reference', 'render'):
            raise ValueError('Invalid source role')
        if file.get('language') is not None and not re.fullmatch('[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', file['language']):
            raise ValueError('Invalid language')
        locator = file['locator']
        if locator['kind'] == 'chunks':
            size = 0
            for chunk in locator['chunks']:
                valid_hash(chunk['sha256'])
                if type(chunk['bytes']) is not int or not 0 < chunk['bytes'] <= CHUNK:
                    raise ValueError('Invalid source chunk size')
                size += chunk['bytes']
            if size != file['bytes']:
                raise ValueError('Source chunk sizes do not match file')
        elif locator['kind'] == 'snapshot':
            valid_hash(locator['snapshot_id'])
            if not safe_path(locator['source_path']).startswith('files/'):
                raise ValueError('Snapshot sources must be regular archived files')
        else:
            raise ValueError('Unknown source locator')
    if total > MAX_BYTES:
        raise ValueError('Source project exceeds restore limit')
    paths = {f['path']: f for f in files}
    tree_hash = sha(canonical([{k: f[k] for k in ('path', 'sha256', 'bytes')} for f in files]))
    if manifest.get('git', {}).get('source_tree_sha256') != tree_hash:
        raise ValueError('Selected source tree checksum mismatch')
    if manifest['git'].get('working_tree') not in ('clean', 'dirty', 'unknown'):
        raise ValueError('Explicit Git working-tree state required')
    recipe = manifest['recipe']
    if recipe.get('entrypoint') is not None and recipe['entrypoint'] not in paths:
        raise ValueError('Recipe entrypoint is absent')
    if not isinstance(recipe.get('command'), list) or not all(isinstance(x, str) for x in recipe['command']) or not isinstance(recipe.get('tools'), dict):
        raise ValueError('Explicit recipe and tool versions required (empty if unknown)')
    for dep in manifest['dependencies']:
        if dep.get('status') not in ('present', 'missing', 'excluded', 'unknown', 'declared') or not dep.get('name'):
            raise ValueError('Invalid dependency evidence')
        if dep['status'] == 'present' and (dep.get('path') not in paths or dep.get('sha256') != paths[dep['path']]['sha256']):
            raise ValueError('Present dependencies must pin a file checksum')
    for parent in manifest.get('lineage', []):
        if parent.get('app_id') != manifest['app_id'] or not isinstance(parent.get('video_id'), str):
            raise ValueError('Lineage must remain within the app')
        valid_hash(parent['revision_id'])
    return manifest


def real_path(path):
    """Reject symlinks and Windows reparse points in every existing component."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Traversal is not allowed')
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('Symlinks and reparse points are not allowed')
    return path


def file_hash(path):
    real_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Only regular files are supported')
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest(), info.st_size


@contextmanager
def output_parent(root, relative):
    """Anchor POSIX writes to real directory handles, including mkdir steps."""
    parts = safe_path(relative).split('/')
    if os.name == 'posix':
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in [*root.parts[1:], *parts[:-1]]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1], root.joinpath(*parts)
        finally:
            os.close(fd)
    else:
        parent = real_path(root.joinpath(*parts[:-1]))
        parent.mkdir(parents=True, exist_ok=True)
        real_path(parent)
        yield None, parts[-1], parent / parts[-1]


def restore(revision, destination, fetch, *, resume=False, missing_only=False, dry_run=False):
    manifest = validate(revision)
    root = real_path(destination)
    marker = root / '.outpost-restore.json'
    identity = {'app_id': manifest['app_id'], 'revision_id': revision['revision_id']}
    if root.exists():
        if not root.is_dir():
            raise ValueError('Restore destination must be a directory')
        if resume:
            real_path(marker)
            if not marker.exists() or json.loads(marker.read_text()) != identity:
                raise ValueError('Resume requires the same source revision')
        elif not missing_only:
            raise FileExistsError('Use a fresh destination, --resume, or --missing-only')
    elif resume or missing_only:
        raise ValueError('Resume/checkout destination does not exist')
    # Inspect every existing target before downloading or creating anything.
    planned = []
    for file in manifest['files']:
        path = real_path(root / file['path'])
        if path.exists():
            if file_hash(path) != (file['sha256'], file['bytes']):
                raise FileExistsError('Existing file differs; restore refuses to overwrite: ' + file['path'])
        else:
            planned.append(file)
    result = {**identity, 'dry_run': dry_run, 'download_files': len(planned),
              'planned_bytes': sum(f['bytes'] for f in planned), 'verified_files': 0,
              'render_tested': False, 'dependencies': manifest['dependencies']}
    if dry_run:
        return result
    if not root.exists():
        root.mkdir(mode=0o700)  # The caller must choose an existing parent.
    lock = root / '.outpost-restore-lock'
    lock.mkdir(mode=0o700)  # A stale lock after process termination needs explicit inspection/removal.
    try:
        if not missing_only and not marker.exists():
            with marker.open('x') as file:
                json.dump(identity, file)
        for entry in planned:
            with output_parent(root, entry['path']) as (fd, name, path):
                temporary = '.outpost-part-' + os.urandom(12).hex()
                temp_path = path.parent / temporary
                kwargs = {'dir_fd': fd} if fd is not None else {}
                handle = os.open(temporary if fd is not None else temp_path,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, **kwargs)
                try:
                    digest, size = hashlib.sha256(), 0
                    with os.fdopen(handle, 'wb') as output:
                        for block in fetch(entry):
                            size += len(block)
                            if size > entry['bytes']:
                                raise ValueError('Downloaded source exceeds declared size')
                            digest.update(block)
                            output.write(block)
                        if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                            raise ValueError('Downloaded source checksum mismatch')
                        output.flush()
                        os.fsync(output.fileno())
                    if fd is not None:
                        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                    else:
                        real_path(path)
                        os.link(temp_path, path)  # Atomic no-replace on supported local filesystems.
                finally:
                    os.unlink(temporary if fd is not None else temp_path, **kwargs)
        result['verified_files'] = len(manifest['files'])
        result['restore_os'] = os.name
        return result
    finally:
        lock.rmdir()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirects are not allowed for private source requests')


class Reader:
    def __init__(self, origin, key):
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('Use a credential-free HTTPS Outpost origin')
        self.origin = origin.rstrip('/')
        self.opener = build_opener(NoRedirect())
        with self.opener.open(Request(self.origin + '/api/session', data=canonical({'key': key}), headers={'Content-Type': 'application/json'}), timeout=60) as response:
            self.cookie = response.headers['Set-Cookie'].split(';')[0]

    def fetch(self, revision, file):
        manifest = revision['manifest']
        url = self.origin + '/api/sources/' + manifest['app_id'] + '/revision/' + revision['revision_id'] + '/files/' + str(manifest['files'].index(file))
        with self.opener.open(Request(url, headers={'Cookie': self.cookie}), timeout=180) as response:
            while block := response.read(CHUNK):
                yield block


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--key-env', default='OUTPOST_READ_KEY')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--missing-only', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    revision = json.loads(args.manifest.read_bytes())
    validate(revision)
    reader = None if args.dry_run else Reader(args.origin, os.environ[args.key_env])
    print(json.dumps(restore(revision, args.out, lambda file: reader.fetch(revision, file),
                             resume=args.resume, missing_only=args.missing_only, dry_run=args.dry_run), indent=2))


if __name__ == '__main__':
    main()
