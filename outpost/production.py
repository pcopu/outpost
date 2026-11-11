"""Versioned production files and reusable generation assets within an app."""
import hashlib
import json
from pathlib import Path
import tempfile

from . import model
from .util import (canonical, digest, excluded_name, inspect_file, inventory,
                   relative, slug, write_json)


ROLES = {'raw': 'raw', 'sources': 'source', 'audio': 'audio',
         'transcripts': 'transcript', 'renders': 'render', 'references': 'reference'}
KINDS = {'.mp4': 'video', '.mov': 'video', '.webm': 'video', '.mkv': 'video',
         '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
         '.gif': 'image', '.wav': 'audio', '.mp3': 'audio', '.m4a': 'audio',
         '.flac': 'audio', '.ogg': 'audio'}


def initialize_workspace(lib, app, directory, dry_run=False):
    lib.app_dir(app)
    root = Path(directory).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise FileExistsError('Production workspace destination already exists')
    result = {'app_id': app, 'directory': str(root), 'dry_run': dry_run}
    if not dry_run:
        root.mkdir(parents=True)
        (root / 'raw').mkdir()
        (root / 'videos').mkdir()
        write_json(root / 'production.json', {
            'format_version': 1, 'app_id': app, 'raw_directory': 'raw', 'videos': []})
    return result


def _directory(root, name):
    path = root / relative(name)
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_dir():
        raise ValueError('Unsafe or missing production directory')
    return path


def _file(root, name):
    path = root / relative(name)
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('Unsafe or missing production file')
    return path


def _file_metadata(name, sha, role=None, extra=None):
    path = Path(name)
    selected_role = role or ('transcript' if path.suffix.lower() in ('.srt', '.vtt')
                             else ROLES.get(path.parts[0], 'source'))
    result = {'path': name, 'sha256': sha, 'role': selected_role}
    if selected_role == 'transcript':
        result['language'] = path.stem.rsplit('.', 1)[-1] if '.' in path.stem else None
    result.update(extra or {})
    return result


def _collect(root, directory, entries, excluded, role=None):
    folder = _directory(root, directory)
    found, skipped = inventory(folder, business=True)
    excluded.extend({'path': (Path(directory) / e['path']).as_posix(), 'reason': e['reason']}
                    for e in skipped)
    selected = []
    for item in found:
        if item['kind'] != 'file':
            raise ValueError('Production inputs must be regular files, not symlinks')
        name = (Path(directory) / item['path']).as_posix()
        selected.append(_file_metadata(name, item['sha256'], role or ROLES.get(Path(item['path']).parts[0])))
        entries[name] = {'path': str(root / name), 'original_path': str(root / name),
                         'sha256': item['sha256'], 'bytes': item['size'],
                         'media_kind': KINDS.get(Path(name).suffix.lower(), 'document')}
    return selected


def import_production(lib, app, directory, dry_run=False):
    root = Path(directory).expanduser().resolve()
    app_path = lib.app_dir(app)
    if root == app_path or root.is_relative_to(app_path) or app_path.is_relative_to(root):
        raise ValueError('Production workspace and managed app storage must be separate')
    config_path = _file(root, 'production.json')
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    if config.get('format_version') != 1 or config.get('app_id') != app or model.credentials(config):
        raise ValueError('Production app, format or credential validation failed')
    videos = config.get('videos', [])
    ids = [slug(v['id']) for v in videos]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate production video ID')
    if 'raw' in ids:
        raise ValueError('The production ID raw is reserved for reusable assets')
    existing = {e['entity_key'] for e in lib.rows(app, 'source_entities')}
    known = existing | {'production:' + ident for ident in ids}
    for video in videos:
        if video.get('derived_from') and 'production:' + video['derived_from'] not in known:
            raise ValueError('Unknown production parent')
        if any(post not in existing for post in video.get('posts', [])):
            raise ValueError('Unknown publication entity')
    entries, excluded = {}, []
    raw_dir = config.get('raw_directory', 'raw')
    raw = _collect(root, raw_dir, entries, excluded, 'raw')
    raw_by_path = {f['path']: f for f in raw}
    entities, records, links, relationships = [], [], [], []
    groups = [('raw', 'generation_assets', config.get('title', 'Raw generation assets'), raw, {})]
    for video in videos:
        selected = _collect(root, video['directory'], entries, excluded) if video.get('directory') else []
        for item in video.get('files', []):
            name = item['path']
            file = _file(root, name)
            reason = excluded_name(name) or inspect_file(file, business=True)
            if reason:
                raise ValueError('Explicit production file was excluded: ' + name)
            sha = digest(file)
            selected.append(_file_metadata(name, sha, item.get('role'),
                                           {k: v for k, v in item.items() if k not in ('path', 'sha256')}))
            entries[name] = {'path': str(file), 'original_path': str(file), 'sha256': sha,
                             'bytes': file.stat().st_size,
                             'media_kind': KINDS.get(file.suffix.lower(), 'document')}
        for name in video.get('uses', []):
            if name not in raw_by_path:
                raise ValueError('Missing or excluded reusable raw asset')
            selected.append(raw_by_path[name])
        groups.append((video['id'], 'video_production', video.get('title', video['id']), selected, video))
    for ident, kind, title, selected, metadata in groups:
        key = 'production:' + ident
        payload = {'id': ident, 'title': title, 'files': selected, 'metadata': metadata}
        revision = hashlib.sha256(canonical(payload)).hexdigest()
        # The same video can be carried in a later manifest revision. Include
        # that source identity so its immutable record retains the right hash.
        record_id = key + ':' + hashlib.sha256(canonical([revision, config_sha])).hexdigest()
        entities.append({'entity_key': key, 'kind': kind, 'title': title,
                         'files': selected, 'production': metadata, 'revision': revision})
        records.append({'record_id': record_id, 'kind': kind, 'entity_key': key,
                        'source_path': 'production.json', 'source_sha256': config_sha,
                        'capture_context': {}, 'payload': payload,
                        'raw_payload_sha256': revision})
        for position, item in enumerate(selected):
            links.append(dict(item, entity_key=key, record_id=record_id, ordinal=position))
        if metadata.get('derived_from'):
            relationships.append({'from_key': key, 'to_key': 'production:' + metadata['derived_from'],
                                  'relationship': 'derived_from'})
        relationships.extend({'from_key': key, 'to_key': post, 'relationship': 'published_as'}
                             for post in metadata.get('posts', []))
    streams = {'entities': entities, 'records': records, 'asset_links': links,
               'relationships': relationships, 'metrics': []}
    version = hashlib.sha256(canonical({'config': config, 'streams': streams, 'assets': list(entries.values())})).hexdigest()
    manifest = {'format_version': 1, 'app_id': app, 'bundle_id': 'production-' + version,
                'assets': list(entries.values()),
                'raw_sources': [{'path': str(config_path), 'original_path': str(config_path),
                                 'sha256': config_sha, 'bytes': len(config_bytes), 'format': 'json'}],
                'excluded': excluded,
                'provenance': {'kind': 'production_workspace'}}
    with tempfile.TemporaryDirectory(prefix='outpost-production-') as temp:
        staging = Path(temp)
        for name, rows in streams.items():
            file = name + '.jsonl'
            (staging / file).write_bytes(b''.join(canonical(row) + b'\n' for row in rows))
            manifest[name] = file
        write_json(staging / 'bundle.json', manifest)
        result = lib.import_bundle(app, staging, dry_run=dry_run, reference_roots=[root])
    return dict(result, videos=len(videos), raw_assets=len(raw))


def show_production(lib, app, video=None):
    if video is None:
        rows = lib.rows(app, 'source_entities', {'kind': 'video_production'})
        return [json.loads(row['metadata_json']) for row in rows]
    slug(video)
    rows = lib.rows(app, 'source_entities', {'entity_key': 'production:' + video})
    if not rows:
        raise ValueError('Unknown production video')
    return json.loads(rows[0]['metadata_json'])
