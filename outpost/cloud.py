"""Cloud catalog references and lossless D1/R2 value encoding.

The Mac retains connection metadata, never an implicit catalog cache. Large
values stay in private, content-addressed R2 objects because D1 limits row size.
"""
import base64
import hashlib
from http.client import IncompleteRead
import json
import math
from pathlib import Path
import re
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .util import canonical, slug
from .gateway import NoRedirect, credential_token

REFERENCE_PREFIX = 'outpost-r2-value-v1:'
INLINE_LIMIT = 1000000
ROW_LIMIT = 1500000


class CloudRequestError(RuntimeError):
    def __init__(self, status):
        super().__init__(f'Cloud catalog request failed (HTTP {status}); no local fallback was created')
        self.status = status


def _reference(value, put_blob):
    kind = 'text' if isinstance(value, str) else 'blob'
    data = value.encode('utf-8') if kind == 'text' else value
    sha = hashlib.sha256(data).hexdigest()
    if put_blob(data) != sha:
        raise ValueError('Cloud value checksum mismatch')
    return f'{REFERENCE_PREFIX}{kind}:{sha}:{len(data)}'


def encode_value(value, put_blob):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if abs(value) > 2 ** 53 - 1:
            raise ValueError('Cloud integer exceeds exact JSON/D1 transport precision')
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Cloud numeric value must be finite')
        return value
    if isinstance(value, (bytes, str)):
        data = value.encode('utf-8') if isinstance(value, str) else value
        if len(data) > INLINE_LIMIT or isinstance(value, str) and value.startswith(REFERENCE_PREFIX):
            return _reference(value, put_blob)
        return {'$outpost_blob': base64.b64encode(value).decode()} if isinstance(value, bytes) else value
    raise ValueError('Cloud catalog values must be SQLite scalar values')


def encode_row(row, put_blob):
    result = {k: encode_value(v, put_blob) for k, v in row.items()}
    while len(canonical(result)) > ROW_LIMIT:
        candidates = [(len(canonical(result[k])), k) for k, v in row.items()
                      if isinstance(v, (str, bytes)) and not
                      (isinstance(result[k], str) and result[k].startswith(REFERENCE_PREFIX))]
        if not candidates:
            raise ValueError('Cloud row exceeds the bounded row size')
        _, key = max(candidates)
        result[key] = _reference(row[key], put_blob)
    return result


def decode_value(value, get_blob):
    if isinstance(value, dict) and set(value) == {'$outpost_blob'}:
        return base64.b64decode(value['$outpost_blob'], validate=True)
    if not isinstance(value, str) or not value.startswith(REFERENCE_PREFIX):
        return value
    match = re.fullmatch(re.escape(REFERENCE_PREFIX) + r'(text|blob):([a-f0-9]{64}):([0-9]+)', value)
    if not match:
        raise ValueError('Invalid cloud value reference')
    kind, sha, size = match.groups()
    data = get_blob(sha)
    if len(data) != int(size) or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError('Cloud value checksum mismatch')
    return data.decode('utf-8') if kind == 'text' else data


class CloudCatalog:
    def __init__(self, reference):
        allowed = {'format_version','backend','app_id','worker_url','database_id','token_env',
                   'token_file','token_key','original_catalog_sha256','original_snapshot_id','verification_sha256'}
        if not set(reference) <= allowed:
            raise ValueError('Cloud references may contain connection references only, never credential values')
        self.reference = dict(reference)
        self.app = slug(reference['app_id'])
        if reference.get('format_version') != 1 or reference.get('backend') != 'd1':
            raise ValueError('Unsupported cloud catalog reference')
        url = urlsplit(reference['worker_url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('Cloud catalog requires a credential-free HTTPS origin')
        self.url = reference['worker_url'].rstrip('/') + '/api/catalog/' + self.app
        self.token_env = reference.get('token_env', 'OUTPOST_ARCHIVE_TOKEN')
        if not re.fullmatch('[A-Z][A-Z0-9_]*', self.token_env):
            raise ValueError('Invalid cloud credential environment reference')
        self._uploaded = set()

    def token(self):
        return credential_token(self.token_env,self.reference.get('token_file'),
                                self.reference.get('token_key','ADMIN_TOKEN'))

    def request(self, action, data=None, *, method=None, raw=None):
        if not re.fullmatch(r'[a-z-]+(?:/[a-f0-9]{64})?', action):
            raise ValueError('Invalid cloud operation')
        payload = raw if raw is not None else canonical(data) if data is not None else None
        headers = {'Authorization': 'Bearer ' + self.token(), 'User-Agent': 'Outpost cloud/1.0'}
        if payload is not None:
            headers['Content-Type'] = 'application/octet-stream' if raw is not None else 'application/json'
        verb = method or ('POST' if data is not None else 'GET')
        # Init/import/mutate carry durable identities, so transport retries cannot
        # duplicate committed changes. Redirects must never receive credentials.
        for attempt in range(3):
            try:
                with build_opener(NoRedirect()).open(Request(self.url + '/' + action, data=payload,
                        headers=headers, method=verb), timeout=180) as response:
                    result = response.read(64 * 1024 * 1024 + 1)
                if len(result) > 64 * 1024 * 1024:
                    raise ValueError('Cloud response exceeded its bounded size')
                return result if action.startswith('blob/') and verb == 'GET' else json.loads(result)
            except HTTPError as error:
                status = error.code; error.close()
                if status in (429, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt); continue
                raise CloudRequestError(status) from None
            except (URLError, ConnectionError, TimeoutError, IncompleteRead):
                if attempt < 2:
                    time.sleep(1 + attempt); continue
                raise RuntimeError('Cloud connection failed; completion is unconfirmed; reuse the operation identity') from None

    def upload_blob(self, data):
        if len(data) > 64 * 1024 * 1024:
            raise ValueError('Cloud scalar value exceeds 64 MB; retain it as a separate asset')
        sha = hashlib.sha256(data).hexdigest()
        if sha not in self._uploaded:
            self.request('blob/' + sha, method='PUT', raw=data)
            remote = self.read_blob(sha)
            if remote != data:
                raise ValueError('Cloud value read-back checksum mismatch')
            self._uploaded.add(sha)
        return sha

    def read_blob(self, sha):
        value = self.request('blob/' + sha)
        if hashlib.sha256(value).hexdigest() != sha:
            raise ValueError('Cloud value read-back checksum mismatch')
        return value

    def decode_row(self, row):
        return {key: decode_value(value, self.read_blob) for key, value in row.items()}

    def rows(self, table, filters=None, limit=None):
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ValueError('Limit must be positive')
        # Query filters can reference a large value without uploading anything.
        filters = {k: encode_value(v, lambda b: hashlib.sha256(b).hexdigest())
                   for k, v in (filters or {}).items()}
        rows, offset, revision, page_size = [], 0, None, 100
        while limit is None or len(rows) < limit:
            count = min(page_size, limit - len(rows)) if limit is not None else page_size
            try:
                page = self.request('rows', {'table': table, 'filters': filters, 'limit': count, 'offset': offset})
            except CloudRequestError as error:
                if error.status == 413 and count > 1:
                    page_size = max(1, count // 2); continue
                raise
            if revision is not None and page['revision'] != revision:
                raise RuntimeError('Cloud catalog changed during query; retry for a consistent result')
            revision = page['revision']
            rows.extend(self.decode_row(row) for row in page['rows'])
            offset += len(page['rows'])
            if len(page['rows']) < count:
                break
        if self.request('state')['revision'] != revision:
            raise RuntimeError('Cloud catalog changed during query; retry for a consistent result')
        return rows

    def sql(self, sql, params=()):
        result = self.request('sql', {'sql': sql, 'params': [encode_value(v, lambda b: hashlib.sha256(b).hexdigest()) for v in params]})
        return {'revision': result['revision'], 'rows': [self.decode_row(row) for row in result['rows']]}

    def mutate(self, statements, expected_revision, operation_id, dry_run=False):
        from .model import credentials
        if credentials([s.get('params', []) for s in statements]):
            raise ValueError('Possible credential in cloud transaction; values withheld')
        put = (lambda b: hashlib.sha256(b).hexdigest()) if dry_run else self.upload_blob
        encoded = [{'sql': s['sql'], 'params': [encode_value(v, put) for v in s.get('params', [])]} for s in statements]
        return self.request('mutate', {'statements': encoded, 'expected_revision': expected_revision,
                                      'operation_id': operation_id, 'dry_run': dry_run})

    def record(self, table, row, dry_run=False):
        from .library import DOMAIN_TABLES
        from .util import secret_data
        if table not in DOMAIN_TABLES or not row or secret_data(row):
            raise ValueError('Invalid record, table, or possible credential')
        schema = self.request('schema')['schema']
        entry = next((s for s in schema if s['name'] == table and s['type'] == 'table'), None)
        if not entry:
            raise ValueError('Unknown cloud table')
        columns = {c['name'] for c in entry['columns']}
        keys = [c['name'] for c in sorted(entry['columns'], key=lambda c: c['pk']) if c['pk']]
        if not row.keys() <= columns or not keys or not all(k in row for k in keys):
            raise ValueError('Unknown column or missing primary key')
        names = ','.join('"' + c + '"' for c in row)
        update = ','.join('"' + c + '"=excluded."' + c + '"' for c in row if c not in keys)
        clause = 'DO UPDATE SET ' + update if update else 'DO NOTHING'
        key_sql = ','.join('"' + k + '"' for k in keys)
        sql = f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in row)}) ON CONFLICT({key_sql}) {clause}'
        revision = self.request('state')['revision']
        result = self.mutate([{'sql': sql, 'params': list(row.values())}], revision, 'record-' + uuid.uuid4().hex, dry_run)
        return dict(result, table=table, primary_key={k: row[k] for k in keys})


def for_app(lib, app):
    path = lib.app_dir(app) / 'cloud.json'
    if path.is_symlink():
        raise ValueError('Symlink cloud catalog reference')
    if not path.exists():
        return None
    reference = json.loads(path.read_text())
    if reference.get('app_id') != app:
        raise ValueError('Cloud catalog reference belongs to another app')
    return CloudCatalog(reference)


def validate_paths(lib, app, remote, table, row):
    from .library import PATH_COLUMNS
    from .util import relative
    selected = [row[k] for k in PATH_COLUMNS.get(table, ()) if row.get(k)]
    if not selected:
        return
    archived = set()
    for path in (lib.app_dir(app) / 'snapshots').glob('*/manifest.json'):
        manifest = json.loads(path.read_text())
        if manifest.get('app_id') != app:
            raise ValueError('Archive mapping belongs to another app')
        archived.update(e['path'] for e in manifest['entries'] if e['kind'] == 'file')
    for value in selected:
        relative(value)
        if value not in archived and not remote.rows('vault_assets', {'stored_path': value}, 1):
            raise ValueError('Cloud catalog paths must reference an archived asset; import the asset first')
