"""Small filesystem, validation, and SQLite safety primitives."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def slug(value):
    if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', value):
        raise ValueError('App ID must be lowercase ASCII words separated by hyphens, at most 63 characters')
    return value


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or any(x in ('', '.', '..') for x in value.split('/')) or '\\' in value or '\0' in value:
        raise ValueError('Unsafe relative path')
    return p


def local_filesystem(path):
    # Fail closed for known object/network mounts. Normal local APFS/ext filesystems work.
    output = subprocess.run(['mount'], capture_output=True, text=True, check=True).stdout
    target = str(Path(path).resolve())
    for line in output.splitlines():
        m = re.search(r' on (.*?) (?:type (\S+) |\()(.*)', line)
        if m and (target == m[1] or target.startswith(m[1].rstrip('/') + '/')):
            kind = (str(m[2]) + ' ' + m[3]).lower()
            if any(x in kind for x in ('rclone', 's3fs', 'fuse', 'nfs', 'smbfs', 'cifs', 'webdav')):
                raise ValueError('The live catalog must reside on a local filesystem, not an object/network mount')


@contextmanager
def lock(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Symlink lock path')
    with path.open('a+b') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError('Another writer is active; retry after it finishes') from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path, readonly=False):
    db = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5, factory=ManagedConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    else:
        db.execute('PRAGMA synchronous=FULL')
    return db


def check_database(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('SQLite foreign key check failed')


def backup_database(source, destination):
    src = connect(source, readonly=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        check_database(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
        dst.commit()
    finally:
        dst.close()
        src.close()


SECRET = re.compile(
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
    rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|'
    rb'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|'
    rb'(?i:bearer\s+[A-Za-z0-9_\-.]{24,})|'
    rb'(?i:(?:api[_-]?key|api[_-]?token|access[_-]?token|client[_-]?secret|secret[_-]?access[_-]?key|password)'
    rb'["\x27]?\s*[:=]\s*["\x27][A-Za-z0-9_\-./+=]{20,}["\x27])'
)


def secret_bytes(data):
    # Most media blocks contain none of the literal prefixes required by the
    # credential regex. C-level substring checks avoid scanning every binary
    # byte with the more expensive expression while preserving its matches.
    probe=data.lower()
    prefixes=(b'private key',b'akia',b'asia',b'eyj',b'bearer',b'password',
              b'api_key',b'api-key',b'apikey',b'api_token',b'api-token',b'apitoken',
              b'access_token',b'access-token',b'accesstoken',
              b'client_secret',b'client-secret',b'clientsecret',
              b'secret_access_key',b'secret-access-key',b'secretaccesskey')
    if not any(prefix in probe for prefix in prefixes):
        return False
    return bool(SECRET.search(data))


def secret_field(key,value):
    fields={'apikey','apitoken','accesstoken','refreshtoken','cookie','cookies',
            'session','sessionid','sessiontoken','authorization','password',
            'clientsecret','privatekey','secretaccesskey','token','csrftoken'}
    return (re.sub('[^a-z]','',str(key).lower()) in fields and
            isinstance(value,(str,dict,list)) and bool(value) and
            value!='[REDACTED CREDENTIAL]')


def secret_data(value):
    if isinstance(value,str):
        if secret_bytes(value.encode()):
            return True
        if value.lstrip().startswith(('{','[')):
            try:
                return secret_data(json.loads(value))
            except ValueError:
                pass
        return False
    if isinstance(value,dict):
        return secret_bytes(canonical(value)) or any(secret_field(k,v) or secret_data(v) for k,v in value.items())
    if isinstance(value,(list,tuple)):
        return any(secret_data(v) for v in value)
    return False


def excluded_name(rel):
    p = PurePosixPath(rel)
    for part in p.parts:
        name = part.lower()
        if name in ('.git', '.aws', '.ssh', '.codex', '.agents', 'node_modules', '__pycache__', '.venv', '.ds_store') or name.endswith(('.pyc', '.pyo')):
            return 'cache, tooling state, or hidden credential directory'
        if name.startswith(('.env', '.dev.vars')) or any(x in name for x in ('credential', 'keychain', 'cookie', 'session', 'oauth')) or name.endswith(('.pem', '.key', '.p12', '.pfx', '.mobileprovision')):
            return 'credential or session filename'
    if p.suffix.lower() in ('.db', '.sqlite', '.sqlite3') or str(p).endswith(('-wal', '-shm', '-journal')):
        return 'database not selected for consistent marketing backup'
    return None


def inspect_file(path):
    path=Path(path)
    if path.suffix.lower()=='.json' and path.stat().st_size<=32*1024*1024:
        try:
            if secret_data(json.loads(path.read_text())):
                return 'possible embedded credential (value withheld)'
        except (ValueError,UnicodeError):
            pass
    overlap = b''
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            if secret_bytes(overlap + block):
                return 'possible embedded credential (value withheld)'
            overlap = block[-4096:]
    return None


def inventory(root, skip=()):
    """Never dereference directory symlinks; keep only safe in-tree links."""
    root = Path(root).resolve()
    entries, excluded = [], []
    for base, dirs, files in os.walk(root, followlinks=False):
        names = sorted(set(dirs + files))
        for name in names:
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if rel in skip:
                if name in dirs:
                    dirs.remove(name)
                continue
            why = excluded_name(rel)
            if why:
                excluded.append({'path': rel, 'reason': why})
                if name in dirs:
                    dirs.remove(name)
                continue
            st = path.lstat()
            if path.is_symlink():
                if name in dirs:
                    dirs.remove(name)
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or not resolved.exists() or resolved.is_dir():
                    raise ValueError(f'Unsafe, broken, or directory symlink: {rel}')
                target = os.path.relpath(resolved, path.parent)
                entries.append({'path':rel, 'kind':'symlink', 'target':target, 'mode':stat.S_IMODE(st.st_mode), 'mtime_ns':st.st_mtime_ns})
            elif path.is_file():
                why = inspect_file(path)
                if why:
                    excluded.append({'path':rel, 'reason':why})
                else:
                    entries.append({'path':rel,'kind':'file','sha256':digest(path),'size':st.st_size,'mode':stat.S_IMODE(st.st_mode),'mtime_ns':st.st_mtime_ns})
            elif not path.is_dir():
                raise ValueError(f'Unsupported special file: {rel}')
    kept = {x['path'] for x in entries}
    for entry in entries[:]:
        if entry['kind'] == 'symlink':
            resolved = (root / entry['path']).resolve().relative_to(root).as_posix()
            if resolved not in kept:
                entries.remove(entry)
                excluded.append({'path':entry['path'],'reason':'symlink target excluded'})
    return sorted(entries, key=lambda x:x['path']), sorted(excluded, key=lambda x:x['path'])


def copy_entries(source, destination, entries, overrides=None):
    import shutil
    source, destination = Path(source), Path(destination)
    for entry in entries:
        rel = relative(entry['path'])
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if entry['kind'] == 'file':
            copy_file((overrides or {}).get(str(rel),source / rel), dst)
            if digest(dst) != entry['sha256']:
                raise ValueError(f'Source changed while copying: {rel}')
            os.chmod(dst, entry['mode'])
            os.utime(dst, ns=(entry['mtime_ns'],entry['mtime_ns']))
        else:
            dst.symlink_to(entry['target'])
            os.utime(dst, ns=(entry['mtime_ns'],entry['mtime_ns']), follow_symlinks=False)


def copy_file(source, destination):
    # APFS clones have independent inodes and copy-on-write data. They preserve
    # originals while avoiding a second full media allocation on a small Mac.
    if sys.platform=='darwin':
        import ctypes
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.clonefile(os.fsencode(source),os.fsencode(destination),0)==0:
            return
    import shutil
    shutil.copyfile(source,destination,follow_symlinks=False)


def write_json(path, data):
    path = Path(path)
    with path.open('xb') as f:
        f.write(canonical(data) + b'\n')
        f.flush()
        os.fsync(f.fileno())


def sanitize_json(value):
    """Return a derivative, never token values; preserve the original elsewhere."""
    changes=0
    def visit(node):
        nonlocal changes
        if isinstance(node,str):
            if secret_data(node):
                changes+=1
                return '[REDACTED CREDENTIAL]'
            return node
        if isinstance(node,list):
            return [visit(item) for item in node]
        if isinstance(node,dict):
            result={}
            for key,item in node.items():
                if secret_field(key,item) or (isinstance(item,str) and secret_bytes(canonical({key:item}))):
                    result[key]='[REDACTED CREDENTIAL]';changes+=1
                else:
                    result[key]=visit(item)
            return result
        return node
    result=visit(value)
    return result,changes
