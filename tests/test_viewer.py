import json
from pathlib import Path
import tempfile
import unittest
from outpost.library import Library
try:
    from outpost.viewer import build_project
except ImportError:
    build_project = None

class ViewerExportTests(unittest.TestCase):
    def test_export_preserves_sources_versions_and_app_identity(self):
        self.assertIsNotNone(build_project, 'Viewer export is implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            lib = Library(base / 'library')
            lib.register('alpha', 'Alpha')
            workspace = base / 'production'; (workspace / 'raw').mkdir(parents=True)
            (workspace / 'render.mp4').write_bytes(b'first render')
            (workspace / 'captions.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
            (workspace / 'production.json').write_text(json.dumps({'format_version': 1, 'app_id': 'alpha', 'videos': [{'id': 'film', 'files': [{'path': 'render.mp4', 'role': 'render'}, {'path': 'captions.en.srt', 'role': 'transcript'}]}]}))
            from outpost.production import import_production
            import_production(lib, 'alpha', workspace)
            (workspace / 'render.mp4').write_bytes(b'second render')
            import_production(lib, 'alpha', workspace)
            snap = lib.snapshot('alpha')
            path = Path(snap['path']); manifest = json.loads((path / 'manifest.json').read_text())
            files = []; offset = 0
            for entry in manifest['entries']:
                if entry['kind'] == 'file':
                    files.append({'path': entry['path'], 'offset': offset, 'size': entry['size']}); offset += entry['size']
            transport = {'format_version': 2, 'chunk_size': 128 * 1024 * 1024, 'files': files, 'chunks': [{'path': 'payload/000000', 'size': 99, 'raw_size': offset, 'raw_sha256': 'a'*64, 'sha256': 'b'*64, 'encoding': 'gzip'}], 'total_bytes': offset}
            result = build_project(path, transport)
            self.assertEqual('alpha', result['id'])
            self.assertEqual('Alpha', result['name'])
            self.assertEqual(1, len(result['videos']))
            renders = [f for f in result['videos'][0]['files'] if f['role'] == 'render']
            self.assertEqual(2, len({f['sha256'] for f in renders}))
            self.assertTrue(any(f.get('language') == 'en' for f in result['videos'][0]['files']))
            self.assertTrue(all(a['location']['parts'] for a in result['assets']))
            self.assertEqual([], result['posts'])
            with self.assertRaises(ValueError): build_project(path, dict(transport, total_bytes=offset+1))
            (path/'catalog.sqlite3').chmod(0o600)
            (path/'catalog.sqlite3').write_bytes(b'corrupt')
            with self.assertRaises(ValueError): build_project(path, transport)

    def test_exports_baseline_catalogs_created_before_source_history_tables(self):
        self.assertIsNotNone(build_project)
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as temp:
            lib = Library(Path(temp) / 'library')
            lib.register('older', 'Older library')
            with closing(sqlite3.connect(lib.app_dir('older') / 'catalog.sqlite3')) as db:
                db.execute('DROP VIEW source_records')
                for table in ['source_asset_links', 'source_relationships', 'source_metrics', 'source_entities', 'source_import_files', 'source_imports', 'source_events']:
                    db.execute('DROP TABLE ' + table)
            snap = lib.snapshot('older')
            root = Path(snap['path'])
            manifest = json.loads((root / 'manifest.json').read_text())
            files = []; offset = 0
            for entry in manifest['entries']:
                if entry['kind'] == 'file':
                    files.append({'path': entry['path'], 'offset': offset, 'size': entry['size']})
                    offset += entry['size']
            transport = {'format_version': 1, 'chunk_size': 128*1024*1024, 'files': files, 'chunks': [{'path': 'payload/000000', 'size': offset, 'sha256': 'a'*64}], 'total_bytes': offset}
            result = build_project(root, transport)
            self.assertEqual('older', result['id'])
            self.assertEqual([], result['videos'])
