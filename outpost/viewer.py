"""Build private browser catalogs from checked, immutable app snapshots."""
import argparse
import json
import mimetypes
from pathlib import Path

from .archive import validate_manifest
from .fetch import _transport
from .util import canonical, connect, digest, relative


def _location(app, snapshot, item, transport):
    parts = []
    position, remaining = item['offset'], item['size']
    size = transport['chunk_size']
    while remaining:
        index = position // size
        length = min(remaining, size - position % size)
        chunk = transport['chunks'][index]
        parts.append({'key': f'apps/{app}/snapshots/{snapshot}/' + chunk['path'],
                      'offset': position % size, 'length': length,
                      'encoding': chunk.get('encoding')})
        position += length
        remaining -= length
    return {'binding': 'MEDIA', 'parts': parts}


def build_project(snapshot_directory, transport, locations=None):
    root = Path(snapshot_directory).resolve()
    manifest = validate_manifest(json.loads((root / 'manifest.json').read_text()))
    declared = {e['path']: e for e in manifest['entries'] if e['kind'] == 'file'}
    if digest(root / 'catalog.sqlite3') != declared['catalog.sqlite3']['sha256']:
        raise ValueError('Snapshot catalog checksum mismatch')
    _transport(transport, manifest)
    files = {item['path']: item for item in transport['files']}
    app = manifest['app_id']
    metadata = json.loads((root / 'app.json').read_text())
    if metadata['app_id'] != app or digest(root / 'app.json') != declared['app.json']['sha256']:
        raise ValueError('Snapshot app identity mismatch')
    with connect(root / 'catalog.sqlite3', readonly=True) as db:
        available = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        tables = ['source_entities', 'source_records', 'source_relationships',
                  'source_metrics', 'videos', 'ad_usage', 'vault_asset_sources']
        rows = {table: [dict(r) for r in db.execute('SELECT * FROM "' + table + '"')]
                if table in available else [] for table in tables}
        entities, records = rows['source_entities'], rows['source_records']
        relationships, metrics = rows['source_relationships'], rows['source_metrics']
        legacy, ads, sources = rows['videos'], rows['ad_usage'], rows['vault_asset_sources']
    names = {r['sha256']: r['source_path'] for r in sources}
    by_hash = {}
    for name, entry in declared.items():
        if not name.startswith('files/'):
            continue
        sha = entry['sha256']
        if sha in by_hash:
            continue
        source_name = names.get(sha, name)
        override = (locations or {}).get(sha)
        if override:
            if override.get('binding') not in ('MEDIA', 'ARCHIVE') or override.get('bytes') != entry['size']:
                raise ValueError('Invalid direct media mapping')
            relative(override['key'])
        mime = mimetypes.guess_type(source_name)[0] or 'application/octet-stream'
        if Path(source_name).suffix.lower() in ('.srt', '.vtt', '.md', '.txt'):
            mime = 'text/plain; charset=utf-8'
        by_hash[sha] = {'sha256': sha, 'name': source_name, 'bytes': entry['size'], 'mime': mime,
                        'location': override or _location(app, manifest['snapshot_id'], files[name], transport)}
    videos = []
    for entity in entities:
        if entity['kind'] != 'video_production':
            continue
        detail = json.loads(entity['metadata_json'])
        history = [json.loads(r['payload_json']) for r in records if r['entity_key'] == entity['entity_key'] and r['kind'] == 'video_production']
        items = detail.get('files', []) + [f for h in history for f in h.get('files', [])]
        unique = {canonical(f): f for f in items}
        videos.append({'id': entity['entity_key'], 'title': entity['title'] or entity['entity_key'],
                       'files': list(unique.values()), 'metadata': detail.get('production', {}),
                       'revisions': len(history)})
    path_hash = {p: e['sha256'] for p, e in declared.items()}
    for video in legacy:
        items = []
        for column, role in [('video_path', 'render'), ('audio_path', 'audio'), ('subtitle_path', 'transcript'), ('thumbnail_path', 'raw'), ('raw_metadata_path', 'source')]:
            path = video.get(column)
            if path and path in path_hash:
                items.append({'path': path, 'sha256': path_hash[path], 'role': role})
        videos.append({'id': video['video_id'], 'title': video.get('title') or video.get('caption') or video['video_id'], 'files': items, 'metadata': video, 'revisions': 1})
    raw = []
    for entity in entities:
        if entity['kind'] == 'generation_assets':
            raw.extend(json.loads(entity['metadata_json']).get('files', []))
            for record in records:
                if record['entity_key'] == entity['entity_key'] and record['kind'] == 'generation_assets':
                    raw.extend(json.loads(record['payload_json']).get('files', []))
    raw = list({canonical(file): file for file in raw}.values())
    posts = []
    for entity in entities:
        if entity['kind'] not in ('post', 'organic_post'):
            continue
        detail = json.loads(entity['metadata_json'])
        observations = [{k: r.get(k) for k in ('record_id', 'kind', 'captured_at', 'source_path', 'source_coordinate', 'source_sha256')}
                        for r in records if r['entity_key'] == entity['entity_key']]
        post = detail.get('post') or detail.get('metadata') or {}
        related = next((r['from_key'] for r in relationships if r['to_key'] == entity['entity_key'] and r['relationship'] == 'published_as'), None)
        posts.append({'id': entity['entity_key'], 'videoId': related or entity['source_id'],
                      'title': entity['title'], 'platform': entity['platform'], 'url': entity['source_url'],
                      'status': post.get('status', 'unknown'), 'postedAt': entity['published_at'] or post.get('postedAt'),
                      'postedDate': post.get('publication_date') or post.get('postedDate'),
                      'scheduledAt': post.get('scheduled_at') or post.get('scheduledAt'),
                      'observations': observations, 'metadata': detail,
                      'metrics': [{k: r.get(k) for k in ('key', 'metric', 'value_number', 'value_text', 'unit', 'captured_at', 'source_coordinate')}
                                  for r in metrics if r['entity_key'] == entity['entity_key']]})
    if not raw:
        raw = [{'path': a['name'], 'sha256': a['sha256'], 'role': 'raw'}
               for a in by_hash.values() if a['mime'].startswith(('image/', 'audio/'))]
    relationship_fields = ('from_key', 'to_key', 'relationship', 'record_id')
    return {'format_version': 1, 'id': app, 'name': metadata['name'],
            'snapshot_id': manifest['snapshot_id'], 'catalog_sha256': declared['catalog.sqlite3']['sha256'],
            'videos': videos, 'raw': raw, 'assets': list(by_hash.values()), 'posts': posts,
            'ads': ads, 'relationships': [{k: r.get(k) for k in relationship_fields} for r in relationships], 'held_files': len(manifest.get('held', []))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--transport', type=Path, required=True)
    parser.add_argument('--locations', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    result = build_project(args.snapshot, json.loads(args.transport.read_text()),
                           json.loads(args.locations.read_text()) if args.locations else None)
    if args.out.exists():
        raise FileExistsError('Viewer export destination already exists')
    if not args.dry_run:
        with args.out.open('xb') as output:
            output.write(canonical(result))
    print(json.dumps({'app_id': result['id'], 'videos': len(result['videos']), 'assets': len(result['assets']),
                      'posts': len(result['posts']), 'dry_run': args.dry_run}))


if __name__ == '__main__':
    main()
