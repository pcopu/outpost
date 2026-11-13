import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from outpost.library import Library
from outpost.storage import DirectoryStore, download, upload


class OffloadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('outpost.offload'),
                             'Verified cloud offload is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.lib = Library(self.base / 'data')
        self.lib.register('alpha', 'Alpha')
        self.lib.register('beta', 'Beta')
        self.original = self.base / 'original.mp4'
        self.original.write_bytes(b'original media' * 1000)
        self.lib.import_asset('alpha', self.original)
        self.asset = self.lib.rows('alpha', 'vault_assets')[0]
        self.live = self.lib.app_dir('alpha') / self.asset['stored_path']

        class Packed(DirectoryStore):
            packed = True
            chunk_size = 4096

        self.store = Packed(self.base / 'remote')

    def offload(self, **kwargs):
        from outpost.offload import offload
        return offload(self.lib, 'alpha', self.store, **kwargs)

    def restore(self, result, name='restored'):
        destination = self.base / (name + '-download')
        download(self.store, 'alpha', result['snapshot_id'], destination)
        restored = Library(self.base / name)
        restored.restore(destination)
        return restored

    def test_uploads_missing_and_preserves_catalog_and_recovery(self):
        result = self.offload()
        self.assertTrue(result['verified'])
        self.assertFalse(self.live.exists())
        self.assertTrue(self.original.exists(), 'Unselected source originals must stay untouched')
        self.assertEqual([self.asset], self.lib.rows('alpha', 'vault_assets'))
        self.assertGreater(result['deleted_files'], 0)
        app = self.lib.app_dir('alpha')
        self.assertTrue((app / 'offload-recovery.json').is_file())
        self.assertTrue((app / 'snapshots' / result['snapshot_id'] / 'manifest.json').is_file())
        self.assertFalse((app / 'snapshots' / result['snapshot_id'] / 'catalog.sqlite3').exists())
        restored = self.restore(result)
        self.assertEqual(self.original.read_bytes(),
                         (restored.app_dir('alpha') / self.asset['stored_path']).read_bytes())

    def test_dry_run_does_not_change_local_or_remote_state(self):
        before = {str(p.relative_to(self.base)): p.read_bytes()
                  for p in self.base.rglob('*') if p.is_file()}
        result = self.offload(dry_run=True)
        after = {str(p.relative_to(self.base)): p.read_bytes()
                 for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.store.root.exists())

    def test_upload_failure_never_deletes(self):
        with patch.object(self.store, 'put', side_effect=RuntimeError('upload interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.offload()
        self.assertTrue(self.live.exists())
        self.assertTrue(self.original.exists())

    def test_resume_without_transport_keeps_original_chunk_layout(self):
        self._resume_without_transport()

    def test_resume_without_transport_keeps_uncompressed_layout(self):
        self.store.compress_payload = False
        self._resume_without_transport()

    def _resume_without_transport(self):
        put = self.store.put
        def interrupt(key, path):
            if key.endswith('/transport.json'):
                raise RuntimeError('interrupted before transport')
            return put(key, path)
        with patch.object(self.store, 'put', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'interrupted before transport'):
                self.offload()
        chunks = {p: p.read_bytes() for p in self.store.root.rglob('payload/*')}
        self.assertTrue(chunks)
        self.assertTrue(self.live.exists())
        self.store.chunk_size = 8192
        self.store.compress_payload = True
        result = self.offload()
        self.assertTrue(result['verified'])
        self.assertEqual(chunks, {p: p.read_bytes() for p in chunks})
        restored = self.restore(result)
        self.assertEqual(self.original.read_bytes(),
                         (restored.app_dir('alpha') / self.asset['stored_path']).read_bytes())

    def test_damaged_orphan_chunk_never_authorizes_deletion(self):
        snap = self.lib.snapshot('alpha')
        upload(self.lib, 'alpha', snap['snapshot_id'], self.store)
        for name in ('COMMITTED.json', 'transport.json'):
            next(self.store.root.rglob(name)).unlink()
        chunk = next(self.store.root.rglob('000000'))
        chunk.write_bytes(chunk.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'Invalid.*interrupted'):
            self.offload()
        self.assertTrue(self.live.exists())

    def test_remote_corruption_and_missing_commit_never_delete(self):
        snap = self.lib.snapshot('alpha')
        upload(self.lib, 'alpha', snap['snapshot_id'], self.store)
        chunk = next(self.store.root.rglob('000000'))
        chunk.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum|compressed'):
            self.offload()
        self.assertTrue(self.live.exists())
        self.assertTrue((Path(snap['path']) / self.asset['stored_path']).exists())

    def test_offload_is_repeatable_and_new_import_restores_with_old_media(self):
        first = self.offload()
        again = self.offload()
        self.assertEqual(first['snapshot_id'], again['snapshot_id'])
        self.assertEqual(0, again['deleted_files'])
        second = self.base / 'new.wav'
        second.write_bytes(b'new audio revision')
        self.lib.import_asset('alpha', second, 'audio')
        result = self.offload()
        restored = self.restore(result)
        self.assertEqual(self.lib.rows('alpha', 'vault_assets'),
                         restored.rows('alpha', 'vault_assets'))
        self.assertEqual(2, len(restored.rows('alpha', 'vault_assets')))
        self.assertTrue((restored.app_dir('alpha') / self.asset['stored_path']).exists())

    def test_selected_source_uploads_new_files_and_reports_exclusions(self):
        source = self.base / 'sources'
        source.mkdir()
        (source / 'render.mp4').write_bytes(b'previously unimported render')
        (source / '.env').write_text('EXAMPLE=private')
        (source / 'live.sqlite3').write_bytes(b'not a selected consistent database backup')
        preview = self.offload(sources=[source], dry_run=True)
        self.assertEqual(2, len(preview['excluded']))
        self.assertTrue((source / 'render.mp4').exists())
        result = self.offload(sources=[source])
        self.assertFalse((source / 'render.mp4').exists())
        self.assertTrue((source / '.env').exists())
        self.assertTrue((source / 'live.sqlite3').exists())
        restored = self.restore(result)
        self.assertEqual(2, len(restored.rows('alpha', 'vault_assets')))

    def test_changed_source_during_verification_blocks_all_deletion(self):
        source = self.base / 'source'
        source.mkdir()
        media = source / 'clip.mp4'
        media.write_bytes(b'selected source')
        from outpost.offload import verify_remote
        def change(*args, **kwargs):
            result = verify_remote(*args, **kwargs)
            media.write_bytes(b'newer unbacked source')
            return result
        with patch('outpost.offload.verify_remote', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.offload(sources=[source])
        self.assertEqual(b'newer unbacked source', media.read_bytes())
        self.assertTrue(self.live.exists())

    def test_missing_unbacked_file_fails_closed(self):
        self.live.unlink()
        with self.assertRaisesRegex(ValueError, 'missing|unavailable'):
            self.offload()
        self.assertTrue(self.original.exists())

    def test_source_symlink_or_managed_directory_is_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.base / 'data', target_is_directory=True)
        for source in (alias, self.lib.root, self.lib.app_dir('beta')):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.offload(sources=[source])
        self.assertTrue(self.live.exists())

    def test_all_historical_snapshots_remain_restorable(self):
        old = self.lib.snapshot('alpha')
        another = self.base / 'second.jpg'
        another.write_bytes(b'second version')
        self.lib.import_asset('alpha', another)
        result = self.offload()
        old_restored = self.restore({'snapshot_id': old['snapshot_id']}, 'old')
        self.assertEqual(1, len(old_restored.rows('alpha', 'vault_assets')))
        self.assertEqual(2, len(self.restore(result).rows('alpha', 'vault_assets')))

    def test_existing_current_snapshot_payload_is_removed(self):
        snap = self.lib.snapshot('alpha')
        self.offload()
        self.assertFalse((Path(snap['path']) / self.asset['stored_path']).exists())
        self.assertFalse((Path(snap['path']) / 'catalog.sqlite3').exists())

    def test_repeated_offload_retains_source_recovery_mapping(self):
        source = self.base / 'source'
        source.mkdir()
        (source / 'clip.mp4').write_bytes(b'selected media')
        self.offload(sources=[source])
        self.offload()
        recovery = json.loads((self.lib.app_dir('alpha') / 'offload-recovery.json').read_text())
        self.assertEqual(str(source / 'clip.mp4'), recovery['source_files'][0]['path'])

    def test_safe_aliases_survive_repeat_offload_and_restore(self):
        import os
        files = self.lib.app_dir('alpha') / 'files'
        alias = files / 'alias.mp4'
        alias.symlink_to(os.path.relpath(self.live, files))
        self.lib.snapshot('alpha')
        self.offload()
        result = self.offload()
        restored = self.restore(result)
        self.assertEqual(self.original.read_bytes(), (restored.app_dir('alpha') / 'files/alias.mp4').read_bytes())

    def test_archive_cannot_reside_inside_selected_source(self):
        source = self.base / 'source'
        source.mkdir()
        self.store.root = source / 'archive'
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.offload(sources=[source])
        self.assertTrue(self.live.exists())

    def test_private_holds_remain_local_and_are_declared_in_backup(self):
        held = self.lib.app_dir('alpha') / 'files/private-response.json'
        held.write_text('{"private": "preserve locally"}')
        self.lib.snapshot('alpha', held_files=['files/private-response.json'])
        result = self.offload()
        self.assertTrue(held.exists())
        self.assertEqual(1, result['held_files'])
        self.assertFalse(self.live.exists())

    def test_cli_runs_configured_offload_then_retrieves_one_file(self):
        import subprocess
        import sys
        from outpost.configuration import save
        save(self.lib.root, {'format_version':1,'backend':'directory','path':str(self.store.root),'private_confirmed':True})
        command = [sys.executable, '-m', 'outpost', '--root', str(self.lib.root)]
        done = subprocess.run(command + ['offload','alpha'], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        result = json.loads(done.stdout)
        fetched = self.base / 'downloaded.mp4'
        done = subprocess.run(command + ['asset','fetch','alpha',self.asset['stored_path'],
                              '--snapshot',result['snapshot_id'],'--out',str(fetched)], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(self.original.read_bytes(), fetched.read_bytes())

    def test_abandoned_stage_removes_only_exact_verified_copies(self):
        import shutil
        stage = self.lib.app_dir('alpha') / 'snapshots/.building-interrupted'
        stage.mkdir(parents=True)
        shutil.copy2(self.live, stage / 'copied-media')
        (stage / 'unbacked-data').write_bytes(b'not present in any archive')
        result = self.offload()
        self.assertFalse((stage / 'copied-media').exists())
        self.assertTrue((stage / 'unbacked-data').exists())
        self.assertEqual(1, len(result['retained_unmatched_snapshot_files']))

    def test_single_selected_source_preserves_neighboring_work_files(self):
        package = self.base / 'delivered.zip'
        package.write_bytes(b'completed delivery archive')
        script = self.base / 'importer.py'
        script.write_text('# keep working tools local\n')
        result = self.offload(sources=[package])
        self.assertFalse(package.exists())
        self.assertTrue(script.exists())
        self.assertEqual(2, len(self.restore(result).rows('alpha', 'vault_assets')))

    def test_explicit_staging_copies_remove_only_archived_matches(self):
        import shutil
        copies = self.base / 'staging'
        copies.mkdir()
        shutil.copy2(self.original, copies / 'old-download.mp4')
        (copies / 'unarchived.txt').write_bytes(b'not backed up')
        preview = self.offload(copies=[copies], dry_run=True)
        self.assertTrue((copies / 'old-download.mp4').exists())
        self.assertTrue(preview['dry_run'])
        result = self.offload(copies=[copies])
        self.assertFalse((copies / 'old-download.mp4').exists())
        self.assertTrue((copies / 'unarchived.txt').exists())
        self.assertEqual([str(copies / 'unarchived.txt')], result['retained_unmatched_copies'])
        self.assertTrue(self.original.exists())

    def test_copy_cleanup_cannot_select_live_catalog_or_library_parent(self):
        for path in [self.lib.root, self.lib.app_dir('alpha') / 'catalog.sqlite3', self.base]:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.offload(copies=[path])
        self.assertTrue(self.live.exists())


if __name__ == '__main__':
    unittest.main()
