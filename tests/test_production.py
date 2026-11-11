import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import contextlib
import io

from outpost.library import Library


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lib = Library(self.base / 'library')
        self.lib.register('alpha', 'Alpha')
        self.lib.register('beta', 'Beta')
        self.assertIsNotNone(importlib.util.find_spec('outpost.production'), 'Production workspaces are not implemented')

    def workspace(self):
        root = self.base / 'workspace'
        for folder in ['raw', 'videos/main/sources', 'videos/main/transcripts', 'videos/short/renders']:
            (root / folder).mkdir(parents=True)
        (root / 'raw/landscape.jpg').write_bytes(b'original raw image')
        (root / 'videos/main/sources/scene.py').write_text('# editable scene\n')
        (root / 'videos/main/transcripts/captions.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
        (root / 'videos/main/transcripts/captions.ro.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nSalut\n')
        (root / 'videos/short/renders/revision-1.mp4').write_bytes(b'first render')
        manifest = {'format_version': 1, 'app_id': 'alpha', 'raw_directory': 'raw', 'videos': [
            {'id': 'main', 'title': 'Main film', 'directory': 'videos/main', 'uses': ['raw/landscape.jpg'],
             'posts': ['example:post:1']},
            {'id': 'short', 'title': 'Short cut', 'directory': 'videos/short', 'derived_from': 'main',
             'uses': ['raw/landscape.jpg']}]}
        (root / 'production.json').write_text(json.dumps(manifest))
        return root

    def test_import_tracks_transcripts_sources_raw_reuse_and_revisions(self):
        from outpost.production import import_production, show_production
        root = self.workspace()
        # Existing publication entities can be connected to production sources.
        from outpost.util import connect
        with connect(self.lib.app_dir('alpha') / 'catalog.sqlite3') as db:
            db.execute("INSERT INTO source_entities(entity_key,kind,metadata_json) VALUES ('example:post:1','organic_post','{}')")
        result = import_production(self.lib, 'alpha', root)
        self.assertEqual(2, result['videos'])
        self.assertEqual(6, len(self.lib.rows('alpha', 'vault_assets')))
        manifest_sha = hashlib.sha256((root / 'production.json').read_bytes()).hexdigest()
        self.assertIn(manifest_sha, {a['sha256'] for a in self.lib.rows('alpha', 'vault_assets')})
        self.assertEqual([], self.lib.rows('beta', 'source_entities'))
        main = show_production(self.lib, 'alpha', 'main')
        self.assertEqual({'raw', 'source', 'transcript'}, {f['role'] for f in main['files']})
        self.assertEqual({'en', 'ro'}, {f['language'] for f in main['files'] if f['role'] == 'transcript'})
        relationships = self.lib.rows('alpha', 'source_relationships')
        self.assertEqual({'derived_from', 'published_as'}, {r['relationship'] for r in relationships})
        self.assertTrue(import_production(self.lib, 'alpha', root)['already_imported'])
        (root / 'videos/short/renders/revision-1.mp4').write_bytes(b'corrected render')
        import_production(self.lib, 'alpha', root)
        assets = self.lib.rows('alpha', 'vault_assets')
        self.assertEqual(7, len(assets))
        self.assertEqual(2, len(self.lib.rows('alpha', 'source_imports')))
        snapshot = self.lib.snapshot('alpha')
        destination = self.base / 'export'
        self.lib.export_snapshot('alpha', snapshot['snapshot_id'], destination)
        restored = Library(self.base / 'restored')
        restored.restore(destination)
        self.assertEqual(assets, restored.rows('alpha', 'vault_assets'))
        self.assertEqual(main, show_production(restored, 'alpha', 'main'))

    def test_dry_run_is_nonmutating_and_reports_exclusions(self):
        from outpost.production import import_production
        root = self.workspace()
        manifest = json.loads((root / 'production.json').read_text())
        manifest['videos'][0].pop('posts')
        (root / 'production.json').write_text(json.dumps(manifest))
        (root / 'videos/main/.env').write_text('FAL_KEY=local-private-value\n')
        (root / 'videos/main/transcripts/credential.txt').write_text('private file')
        before = (self.lib.app_dir('alpha') / 'catalog.sqlite3').read_bytes()
        result = import_production(self.lib, 'alpha', root, dry_run=True)
        self.assertEqual(before, (self.lib.app_dir('alpha') / 'catalog.sqlite3').read_bytes())
        self.assertEqual(2, len(result['excluded']))
        self.assertEqual([], self.lib.rows('alpha', 'vault_assets'))

    def test_cross_app_escape_missing_reuse_and_bad_relations_fail(self):
        from outpost.production import import_production
        root = self.workspace()
        with self.assertRaises(ValueError):
            import_production(self.lib, 'beta', root)
        manifest_path = root / 'production.json'
        original = json.loads(manifest_path.read_text())
        for field, value in [('directory', '../outside'), ('uses', ['raw/missing.jpg']), ('derived_from', 'missing')]:
            data = json.loads(json.dumps(original))
            data['videos'][0][field] = value
            manifest_path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                import_production(self.lib, 'alpha', root)
            self.assertEqual([], self.lib.rows('alpha', 'source_entities'))

    def test_initialize_creates_raw_space_without_overwriting(self):
        from outpost.production import initialize_workspace
        root = self.base / 'new-workspace'
        initialize_workspace(self.lib, 'alpha', root, dry_run=True)
        self.assertFalse(root.exists())
        initialize_workspace(self.lib, 'alpha', root)
        self.assertTrue((root / 'raw').is_dir())
        self.assertTrue((root / 'videos').is_dir())
        self.assertEqual('alpha', json.loads((root / 'production.json').read_text())['app_id'])
        with self.assertRaises(FileExistsError):
            initialize_workspace(self.lib, 'alpha', root)

    def test_manifest_edits_preserve_unchanged_video_history(self):
        from outpost.production import import_production
        root = self.workspace()
        config = root / 'production.json'
        data = json.loads(config.read_text())
        data['videos'][0].pop('posts')
        config.write_text(json.dumps(data))
        import_production(self.lib, 'alpha', root)
        data['title'] = 'An updated collection title'
        config.write_text(json.dumps(data))
        import_production(self.lib, 'alpha', root)
        self.assertEqual(2, len(self.lib.rows('alpha', 'source_imports')))

    def test_reserved_video_identity_cannot_replace_the_raw_pool(self):
        from outpost.production import import_production
        root = self.workspace()
        config = root / 'production.json'
        data = json.loads(config.read_text())
        data['videos'] = [{'id': 'raw', 'directory': 'videos/main'}]
        config.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            import_production(self.lib, 'alpha', root)
        self.assertEqual([], self.lib.rows('alpha', 'source_entities'))

    def test_cli_exposes_workspace_import_and_inspection(self):
        from outpost.cli import main
        root = self.base / 'cli-workspace'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                status = main(['--root', str(self.lib.root), 'production', 'init', 'alpha', str(root)])
            except SystemExit:
                self.fail('Production commands are not exposed by the CLI')
        self.assertEqual(0, status)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(['--root', str(self.lib.root), 'production', 'import', 'alpha', str(root)]))
            self.assertEqual(0, main(['--root', str(self.lib.root), 'production', 'show', 'alpha']))


if __name__ == '__main__':
    unittest.main()
