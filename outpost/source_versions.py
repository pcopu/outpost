"""Freeze explicit inputs, independently verify uploads, then commit revisions."""
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
from urllib.request import Request, build_opener

from .source_restore import CHUNK, canonical, sha, safe_path, validate, validate_paths, real_path
from .util import secret_bytes, secret_data


def _git(root, files):
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
    result = {'repository': None, 'commit': None, 'subdirectory': None, 'working_tree': 'unknown',
              'source_tree_sha256': sha(canonical([{k: f[k] for k in ('path', 'sha256', 'bytes')} for f in files]))}
    try:
        top = Path(git('rev-parse', '--show-toplevel'))
        result.update(commit=git('rev-parse', 'HEAD'), subdirectory=root.relative_to(top).as_posix(),
                      working_tree='dirty' if git('status', '--porcelain', '--untracked-files=all') else 'clean')
        try:
            repository = git('config', '--get', 'remote.origin.url')
            # Repository references must never carry userinfo credentials.
            if '://' in repository and '@' in repository.split('://', 1)[1].split('/', 1)[0]:
                raise ValueError('Git remote contains credentials')
            result['repository'] = repository
        except subprocess.CalledProcessError:
            pass
    except subprocess.CalledProcessError:
        pass
    return result


def inspect_file(root, spec):
    path = real_path(root / safe_path(spec['path']))
    chunks, digest, size = [], hashlib.sha256(), 0
    if not path.is_file():
        raise ValueError('Source input must be a regular file')
    with path.open('rb') as file:
        tail = b''
        while block := file.read(CHUNK):
            if secret_bytes(tail + block):
                raise ValueError('Possible credential in source input; values withheld')
            tail = block[-512:]
            chunks.append({'sha256': sha(block), 'bytes': len(block)})
            digest.update(block)
            size += len(block)
    return {**{k: spec[k] for k in ('path', 'role', 'language') if k in spec},
            'sha256': digest.hexdigest(), 'bytes': size, 'locator': {'kind': 'chunks', 'chunks': chunks}}


def freeze(directory, spec):
    root = real_path(directory)
    if secret_data(spec):
        raise ValueError('Possible credential in source description')
    validate_paths([f['path'] for f in spec['files']])
    files = []
    for entry in spec['files']:
        if 'locator' in entry:
            if entry['locator']['kind'] != 'snapshot':
                raise ValueError('Freeze local files or explicit historical snapshot references')
            files.append(copy.deepcopy(entry))
        else:
            files.append(inspect_file(root, entry))
    files.sort(key=lambda f: f['path'])
    dependencies = copy.deepcopy(spec.get('dependencies', []))
    by_path = {f['path']: f for f in files}
    for dependency in dependencies:
        if dependency.get('status') == 'present':
            if dependency.get('path') not in by_path:
                raise ValueError('Present dependency is absent from source files')
            dependency['sha256'] = by_path[dependency['path']]['sha256']
    manifest = {'app_id': spec['app_id'], 'video_id': spec['video_id'], 'files': files,
                'dependencies': dependencies, 'recipe': spec.get('recipe', {'entrypoint': None, 'command': [], 'tools': {}}),
                'git': _git(root, files), 'lineage': spec.get('lineage', [])}
    result = {'format_version': 1, 'revision_id': sha(canonical(manifest)), 'label': spec.get('label', ''), 'manifest': manifest}
    validate(result)
    return result


class SourceClient:
    """Use the existing app's credential reference and origin, never new secrets."""
    def __init__(self, catalog):
        self.catalog = catalog
        self.app = catalog.app

    def request(self, action, data=None, *, raw=None, method=None):
        # CloudCatalog owns authentication, bounded responses, redirect refusal,
        # and transport retries. Source operations have durable content IDs.
        from .cloud import CloudCatalog
        client = CloudCatalog(self.catalog.reference)
        client.url = client.url.replace('/api/catalog/', '/api/sources/')
        return client.request(action, data, raw=raw, method=method)

    def fetch(self, revision, file):
        from .gateway import NoRedirect
        index = revision['manifest']['files'].index(file)
        url = self.catalog.url.replace('/api/catalog/', '/api/sources/') + '/revision/' + revision['revision_id'] + '/files/' + str(index)
        with build_opener(NoRedirect()).open(Request(url, headers={'Authorization': 'Bearer ' + self.catalog.token()}), timeout=180) as response:
            while block := response.read(CHUNK):
                yield block


def upload_chunk(client, block, checksum):
    """Read existing immutable bytes first; upload only absent chunks."""
    from .cloud import CloudRequestError
    from urllib.error import HTTPError
    route = 'blob/' + checksum
    try:
        existing = client.request(route)
    except (CloudRequestError, HTTPError, FileNotFoundError) as error:
        if not isinstance(error, FileNotFoundError) and getattr(error, 'status', getattr(error, 'code', None)) != 404:
            raise
        if isinstance(error, HTTPError): error.close()
    else:
        if existing != block:
            raise ValueError('Existing source object checksum mismatch')
        client.request('confirm/' + checksum, {'bytes': len(block)})
        return
    client.request(route, method='PUT', raw=block)
    if client.request(route) != block:
        raise ValueError('Source upload read-back checksum mismatch')


def archived_reader(lib, catalog):
    """Use existing verified snapshot/chunk retrieval, one selected file at a time."""
    from .fetch import fetch_file
    from .gateway import WorkerStore
    ref = catalog.reference
    store = WorkerStore(ref['worker_url'], ref.get('token_env', 'OUTPOST_ARCHIVE_TOKEN'), ref.get('token_file'), ref.get('token_key', 'ADMIN_TOKEN'))
    def read(file):
        with tempfile.TemporaryDirectory(prefix='outpost-source-check-') as temp:
            output = Path(temp).resolve() / 'selected'
            locator = file['locator']
            fetch_file(lib, store, catalog.app, locator['snapshot_id'], locator['source_path'], output)
            with output.open('rb') as source:
                while block := source.read(CHUNK):
                    yield block
    return read


def record_render(client, revision, directory, spec, expected_revision, operation_id, dry_run=False):
    manifest = validate(revision)
    if client.app != manifest['app_id']:
        raise ValueError('Render belongs to another app')
    root = real_path(directory)
    output = inspect_file(root, {'path': spec['path'], 'role': 'render', **({'language': spec['language']} if spec.get('language') else {})})
    # Frozen-input claims are explicit and checked against every local input.
    # Historic output imports default to an unverified association.
    association = spec.get('association', {'status': 'unverified', 'evidence': 'Imported output; source-to-render match has not been verified'})
    if association.get('status') == 'frozen_inputs':
        for file in manifest['files']:
            if file['locator']['kind'] != 'chunks' or inspect_file(root, file) != file:
                raise ValueError('Frozen-input association requires all exact local inputs; use unverified for historical output')
    render = {'app_id': manifest['app_id'], 'video_id': manifest['video_id'],
              'source_revision_id': revision['revision_id'], 'output': output,
              'settings': spec.get('settings', {}), 'association': association}
    if secret_data(render):
        raise ValueError('Possible credential in render description')
    if not dry_run:
        with (root / output['path']).open('rb') as file:
            for chunk in output['locator']['chunks']:
                block = file.read(chunk['bytes'])
                if sha(block) != chunk['sha256']:
                    raise ValueError('Output changed during upload')
                upload_chunk(client, block, chunk['sha256'])
            if file.read(1):
                raise ValueError('Output changed during upload')
    result = client.request('render', {'render': render, 'expected_revision': expected_revision,
                                     'operation_id': operation_id, 'dry_run': dry_run})
    if dry_run:
        return result
    try:
        if client.request('render/' + result['id']) != render:
            raise ValueError('Render projection mismatch')
    except (ValueError, OSError, RuntimeError):
        return dict(result, publication_status='archived_viewer_verification_pending')
    return dict(result, publication_status='archived_and_viewer_verified')


def publish(client, revision, directory, expected_revision, operation_id, dry_run=False, archive_fetch=None):
    manifest = validate(revision)
    if getattr(client, 'app', manifest['app_id']) != manifest['app_id']:
        raise ValueError('Source revision belongs to another app')
    root = real_path(directory)
    # Check all local inputs before any remote write. Recheck streamed chunks
    # below so edits between preflight and upload cannot commit a mixed version.
    for file in manifest['files']:
        if file['locator']['kind'] == 'chunks' and inspect_file(root, file) != file:
            raise ValueError('Source input changed since freeze; freeze a new revision')
    payload = {'revision': revision, 'expected_revision': expected_revision, 'operation_id': operation_id, 'dry_run': dry_run}
    if dry_run:
        return client.request('publish', payload)
    uploaded = set()
    for file in manifest['files']:
        if file['locator']['kind'] == 'snapshot':
            if archive_fetch is None:
                raise ValueError('Historical sources require independent archived-file verification')
            digest, size = hashlib.sha256(), 0
            for block in archive_fetch(file):
                digest.update(block); size += len(block)
            if (digest.hexdigest(), size) != (file['sha256'], file['bytes']):
                raise ValueError('Historical source checksum mismatch')
            continue
        with (root / file['path']).open('rb') as source:
            for chunk in file['locator']['chunks']:
                block = source.read(chunk['bytes'])
                if sha(block) != chunk['sha256'] or len(block) != chunk['bytes']:
                    raise ValueError('Source input changed during publication')
                if chunk['sha256'] not in uploaded:
                    upload_chunk(client, block, chunk['sha256'])
                    uploaded.add(chunk['sha256'])
            if source.read(1):
                raise ValueError('Source input grew during publication')
    result = client.request('publish', payload)
    try:
        projected = client.request('revision/' + revision['revision_id'])
        validate(projected)
        if projected['manifest'] != manifest:
            raise ValueError('Viewer source projection mismatch')
    except (ValueError, OSError, RuntimeError):
        return dict(result, publication_status='archived_viewer_verification_pending', retry_operation_id=operation_id)
    return dict(result, publication_status='archived_and_viewer_verified', verified_files=len(manifest['files']))
