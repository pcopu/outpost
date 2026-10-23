import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.assertIsNotNone(importlib.util.find_spec('outpost.library'), 'Outpost library is not implemented')
        from outpost.library import Library
        self.lib = Library(self.base / 'library')

    def source(self):
        root = self.base / 'ads'
        (root / 'ugc/raw').mkdir(parents=True)
        (root / 'tools').mkdir()
        (root / 'tools/catalog.py').write_text('# original apparatus\n')
        original = root / 'ugc/raw/video.mp4'
        original.write_bytes(b'original video bytes')
        os.chmod(original, 0o640)
        os.utime(original, ns=(1700000000123456789, 1700000000123456789))
        (root / 'ugc/raw/selected.mp4').symlink_to('video.mp4')
        (root / '.env').write_text('API_KEY=do-not-copy\n')
        (root / 'session.json').write_text('{"cookie":"private"}')
        (root / 'unrelated.sqlite3').write_bytes(b'not a marketing DB')
        db = root / 'ugc/catalog.sqlite3'
        c = sqlite3.connect(db)
        c.executescript(Path(__file__).with_name('legacy-schema.sql').read_text())
        c.execute("INSERT INTO accounts(handle,kind) VALUES ('creator','ugc')")
        c.execute("INSERT INTO videos(video_id,account_handle,source_url,video_path,first_seen_at,last_seen_at) VALUES ('video','creator','https://example.com/post','ugc/raw/video.mp4','a','a')")
        c.execute("INSERT INTO comments(comment_id,video_id,text,first_seen_at,last_seen_at) VALUES ('comment','video','hello','a','a')")
        c.execute("INSERT INTO video_metric_snapshots(video_id,captured_at,views) VALUES ('video','a',42)")
        c.execute("INSERT INTO local_assets(asset_id,sha256,canonical_path,media_kind,file_bytes,duplicate_tiktok_video_id,first_seen_at,last_seen_at) VALUES ('asset','abc','ugc/raw/video.mp4','video',20,'video','a','a')")
        c.execute("INSERT INTO local_asset_sources VALUES ('ugc/raw/video.mp4','asset','original','a','a')")
        c.execute("INSERT INTO local_asset_relations VALUES ('asset','video','near_duplicate',0.99,'[]',1,'reviewed')")
        c.execute("INSERT INTO ad_usage(video_id,platform,ad_id) VALUES ('video','meta','ad')")
        c.commit()
        c.close()
        return root

    def test_app_isolation_and_identifier_collision_rejection(self):
        for app in ('alpha', 'alpha-2'):
            self.lib.register(app, app)
        for bad in ('../alpha', 'ALPHA', 'alpha/other', 'a..b', 'alpha_', '', 'a' * 64):
            with self.assertRaises(ValueError, msg=bad):
                self.lib.register(bad, bad)
        with self.assertRaises(ValueError):
            self.lib.register('alpha', 'Different name')
        self.lib.record('alpha', 'accounts', {'handle':'same','kind':'brand'})
        self.assertEqual([], self.lib.rows('alpha-2', 'accounts'))
        self.assertEqual('same', self.lib.rows('alpha', 'accounts')[0]['handle'])

    def test_dry_runs_leave_no_files_or_database_changes(self):
        self.lib.register('alpha', 'Alpha', dry_run=True)
        self.assertFalse(self.lib.root.exists())
        self.lib.register('alpha', 'Alpha')
        source = self.source()
        before = self.inventory(self.lib.root)
        self.lib.import_legacy('alpha', source, dry_run=True)
        self.lib.import_asset('alpha', source / 'ugc/raw/video.mp4', dry_run=True)
        self.lib.record('alpha', 'accounts', {'handle':'x','kind':'brand'}, dry_run=True)
        self.lib.snapshot('alpha', dry_run=True)
        self.assertEqual(before, self.inventory(self.lib.root))

    def test_catalog_export_reads_one_consistent_transaction(self):
        self.lib.register('alpha','Alpha')
        self.lib.record('alpha','accounts',{'handle':'creator','kind':'ugc'})
        result=self.lib.export_catalog('alpha')
        self.assertEqual('alpha',result['app_id'])
        self.assertEqual('creator',result['tables']['accounts'][0]['handle'])
        self.assertIn('vault_asset_sources',result['tables'])

    def test_credential_prefilter_keeps_mixed_separator_matches(self):
        from outpost.util import secret_bytes
        for label in ('secret_access-key','secret-access_key','secretaccess-key'):
            candidate=(label+'="'+'x'*30+'"').encode()
            self.assertTrue(secret_bytes(candidate),label)

    def test_token_metadata_is_redacted_with_original_checksum_provenance(self):
        from outpost.util import digest
        source=self.source()
        token='eyJ'+'a'*30+'.'+'b'*30+'.'+'c'*30
        metadata=source/'ugc/raw/metadata.json'
        metadata.write_text(json.dumps({'caption':'keep this','cookie':'private-session-value','items':[{'caption':'retain nested metadata','media_url':'https://example.com/media?token='+token}]}))
        original_sha=digest(metadata)
        with contextlib.closing(sqlite3.connect(source/'ugc/catalog.sqlite3')) as db:
            db.execute("UPDATE videos SET raw_metadata_path='ugc/raw/metadata.json'")
            db.commit()
        self.lib.register('alpha','Alpha')
        report=self.lib.import_legacy('alpha',source)
        self.assertEqual(1,len(report['sanitized']))
        stored=self.lib.app_dir('alpha')/'files/legacy/ugc/raw/metadata.json'
        self.assertNotIn(token,stored.read_text())
        self.assertNotIn('private-session-value',stored.read_text())
        self.assertEqual('keep this',json.loads(stored.read_text())['caption'])
        self.assertEqual('retain nested metadata',json.loads(stored.read_text())['items'][0]['caption'])
        self.assertEqual(original_sha,digest(metadata))
        provenance=self.lib.rows('alpha','vault_file_provenance',{'action':'redacted'})
        self.assertEqual(original_sha,provenance[0]['source_sha256'])
        self.assertNotEqual(original_sha,provenance[0]['stored_sha256'])
        self.assertTrue(self.lib.import_legacy('alpha',source)['already_imported'])

    @staticmethod
    def inventory(root):
        return {str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns)
                for p in root.rglob('*')}

    def test_asset_import_preserves_original_and_is_idempotent(self):
        self.lib.register('alpha', 'Alpha')
        p = self.base / 'clip.mp4'
        p.write_bytes(b'content')
        result = self.lib.import_asset('alpha', p)
        self.assertEqual(result['asset_id'], self.lib.import_asset('alpha', p)['asset_id'])
        other = self.base / 'same.mp4'
        other.write_bytes(b'content')
        self.lib.import_asset('alpha', other)
        self.assertEqual(1, len(self.lib.rows('alpha', 'vault_assets')))
        self.assertEqual(2, len(self.lib.rows('alpha', 'vault_asset_sources')))
        self.assertEqual(b'content', p.read_bytes())
        p.write_bytes(b'changed source')
        stored = self.lib.app_dir('alpha') / result['stored_path']
        self.assertEqual(b'content', stored.read_bytes())

    def test_import_maps_paths_and_preserves_all_relationships(self):
        source = self.source()
        self.lib.register('sample', 'Sample')
        report = self.lib.import_legacy('sample', source)
        self.assertEqual(1, report['counts']['videos'])
        video = self.lib.rows('sample', 'videos')[0]
        self.assertEqual('files/legacy/ugc/raw/video.mp4', video['video_path'])
        self.assertEqual('video', self.lib.rows('sample', 'comments')[0]['video_id'])
        self.assertEqual(42, self.lib.rows('sample', 'video_metric_snapshots')[0]['views'])
        self.assertEqual('video', self.lib.rows('sample', 'local_asset_relations')[0]['tiktok_video_id'])
        self.assertEqual('ugc/raw/video.mp4', self.lib.rows('sample', 'vault_path_provenance')[0]['original_path'])
        c = sqlite3.connect(self.lib.app_dir('sample') / 'catalog.sqlite3')
        self.assertEqual([], c.execute('PRAGMA foreign_key_check').fetchall())
        c.close()
        self.assertEqual(b'original video bytes', (source / 'ugc/raw/video.mp4').read_bytes())
        self.assertTrue(self.lib.import_legacy('sample', source)['already_imported'])
        self.assertEqual(1, len(self.lib.rows('sample', 'videos')))
        self.assertFalse((self.lib.app_dir('sample') / 'files/legacy/.env').exists())
        self.assertEqual({'.env', 'session.json', 'unrelated.sqlite3'}, {x['path'] for x in report['excluded']})
        self.lib.record('sample', 'accounts', {'handle':'new','kind':'brand'})
        self.assertTrue(self.lib.import_legacy('sample', source)['already_imported'])
        self.assertEqual(2, len(self.lib.rows('sample', 'accounts')))

    def test_consistent_backup_includes_committed_wal_and_leaves_source(self):
        source = self.source()
        db = source / 'ugc/catalog.sqlite3'
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO accounts(handle,kind) VALUES ('wal','brand')")
        writer.commit()
        writer.execute("INSERT INTO accounts(handle,kind) VALUES ('uncommitted','brand')")
        self.lib.register('alpha', 'Alpha')
        self.lib.import_legacy('alpha', source)
        self.assertEqual({'creator','wal'}, {x['handle'] for x in self.lib.rows('alpha','accounts')})
        writer.rollback()
        self.assertEqual(2, writer.execute('SELECT count(*) FROM accounts').fetchone()[0])

    def test_single_writer_and_symlink_app_escape_are_rejected(self):
        self.lib.register('alpha', 'Alpha')
        with self.lib.writer('alpha'):
            with self.assertRaisesRegex(RuntimeError, 'writer'):
                self.lib.record('alpha','accounts',{'handle':'x','kind':'brand'})
        (self.lib.root / 'apps/escape').symlink_to(self.base)
        with self.assertRaises(ValueError):
            self.lib.rows('escape','accounts')

    def test_symlink_file_storage_cannot_escape_app_scope(self):
        self.lib.register('alpha','Alpha')
        external=self.base/'external';external.mkdir()
        (external/'file.mp4').write_bytes(b'external')
        app=self.lib.app_dir('alpha')
        (app/'files/assets').symlink_to(external)
        p=self.base/'clip.mp4';p.write_bytes(b'clip')
        with self.assertRaises(ValueError):
            self.lib.import_asset('alpha',p)
        (app/'files/outside.mp4').symlink_to(external/'file.mp4')
        self.lib.record('alpha','accounts',{'handle':'creator','kind':'ugc'})
        with self.assertRaises(ValueError):
            self.lib.record('alpha','videos',{'video_id':'v','account_handle':'creator','source_url':'https://example.com','video_path':'files/outside.mp4','first_seen_at':'a','last_seen_at':'a'})

    def test_record_is_transactional_and_rejects_secrets_and_unsafe_tables(self):
        self.lib.register('alpha','Alpha')
        self.lib.record('alpha','accounts',{'handle':'creator','kind':'ugc'})
        self.lib.record('alpha','accounts',{'handle':'creator','kind':'brand'})
        self.assertEqual('brand',self.lib.rows('alpha','accounts')[0]['kind'])
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.lib.record('alpha','comments',{'comment_id':'c','video_id':'absent','first_seen_at':'x','last_seen_at':'x'})
        with self.assertRaises(ValueError):
            self.lib.record('alpha','accounts; DROP TABLE videos',{})
        with self.assertRaises(ValueError):
            self.lib.record('alpha','accounts',{'handle':'x','kind':'brand','display_name':'api_'+'key='+json.dumps('sk-'+'x'*40)})
        self.assertEqual(1,len(self.lib.rows('alpha','accounts')))

    def test_roundtrip_preserves_metadata_symlinks_and_catalog(self):
        source=self.source()
        self.lib.register('alpha','Alpha')
        self.lib.import_legacy('alpha', source)
        snap=self.lib.snapshot('alpha')
        exported=self.base/'export'
        self.lib.export_snapshot('alpha',snap['snapshot_id'],exported)
        from outpost.library import Library
        restored=Library(self.base/'restored')
        restored.restore(exported)
        app=restored.app_dir('alpha')
        file=app/'files/legacy/ugc/raw/video.mp4'
        self.assertEqual(0o640,file.stat().st_mode & 0o777)
        self.assertEqual(1700000000123456789,file.stat().st_mtime_ns)
        link=app/'files/legacy/ugc/raw/selected.mp4'
        self.assertTrue(link.is_symlink())
        self.assertEqual('video.mp4',os.readlink(link))
        self.assertEqual('video',restored.rows('alpha','comments')[0]['video_id'])
        with self.assertRaises(FileExistsError):
            restored.restore(exported)

    def test_changed_import_and_external_symlinks_fail_without_overwrite(self):
        source=self.source()
        self.lib.register('alpha','Alpha')
        self.lib.import_legacy('alpha',source)
        (source/'ugc/raw/video.mp4').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            self.lib.import_legacy('alpha',source)
        (source/'outside').symlink_to(self.base/'elsewhere')
        self.lib.register('beta','Beta')
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.lib.import_legacy('beta',source)
        self.assertEqual([],self.lib.rows('beta','accounts'))


if __name__ == '__main__':
    unittest.main()
