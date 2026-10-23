import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.assertIsNotNone(importlib.util.find_spec('outpost.library'),'Outpost library is not implemented')
        from outpost.library import Library
        from outpost.storage import DirectoryStore
        self.lib=Library(self.base/'lib')
        self.lib.register('alpha','Alpha')
        p=self.base/'clip.mp4';p.write_bytes(b'123456789')
        self.lib.import_asset('alpha',p)
        self.snap=self.lib.snapshot('alpha')
        self.store=DirectoryStore(self.base/'objects')

    def test_upload_resume_and_readback_hash_reject_same_size_corruption(self):
        from outpost.storage import upload, DirectoryStore
        class Interrupted(DirectoryStore):
            def put(self,key,path):
                if key.endswith('manifest.json'):
                    raise RuntimeError('interrupted')
                super().put(key,path)
        with self.assertRaisesRegex(RuntimeError,'interrupted'):
            upload(self.lib,'alpha',self.snap['snapshot_id'],Interrupted(self.base/'objects'))
        self.assertEqual([],list((self.base/'objects').rglob('COMMITTED.json')))
        result=upload(self.lib,'alpha',self.snap['snapshot_id'],self.store)
        self.assertTrue(result['verified'])
        self.assertTrue(upload(self.lib,'alpha',self.snap['snapshot_id'],self.store)['verified'])
        asset=next(p for p in (self.base/'objects').rglob('*') if p.is_file() and p.name==self.lib.rows('alpha','vault_assets')[0]['sha256'])
        asset.chmod(0o600);asset.write_bytes(b'000000000')
        with self.assertRaisesRegex(ValueError,'checksum'):
            upload(self.lib,'alpha',self.snap['snapshot_id'],self.store)

    def test_remote_roundtrip_and_upload_dry_run(self):
        from outpost.storage import upload, download
        from outpost.library import Library
        result=upload(self.lib,'alpha',self.snap['snapshot_id'],self.store,dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertFalse((self.base/'objects').exists())
        upload(self.lib,'alpha',self.snap['snapshot_id'],self.store)
        destination=self.base/'download'
        download(self.store,'alpha',self.snap['snapshot_id'],destination)
        restored=Library(self.base/'restore')
        restored.restore(destination)
        self.assertEqual(self.lib.rows('alpha','vault_assets'),restored.rows('alpha','vault_assets'))

    def test_manifest_traversal_and_app_mismatch_are_rejected(self):
        from outpost.archive import verify_snapshot
        from outpost.library import Library
        export=self.base/'export'
        self.lib.export_snapshot('alpha',self.snap['snapshot_id'],export)
        manifest=export/'manifest.json'
        manifest.chmod(0o600)
        data=json.loads(manifest.read_text())
        data['entries'][0]['path']='../../escape'
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            verify_snapshot(export)
        with self.assertRaises(ValueError):
            Library(self.base/'restore').restore(export)
        self.assertFalse((self.base/'escape').exists())

    def test_snapshot_fails_if_a_catalog_file_is_missing(self):
        row=self.lib.rows('alpha','vault_assets')[0]
        p=self.lib.app_dir('alpha')/row['stored_path']
        p.unlink()
        with self.assertRaisesRegex(ValueError,'missing'):
            self.lib.snapshot('alpha')

    def test_packed_upload_resume_and_restored_metadata(self):
        from outpost.storage import upload, download, DirectoryStore
        from outpost.library import Library
        class Packed(DirectoryStore):
            packed=True
            chunk_size=4096
        packed=Packed(self.base/'packed')
        result=upload(self.lib,'alpha',self.snap['snapshot_id'],packed)
        self.assertEqual('packed-v1',result.get('transport'))
        self.assertTrue(list((self.base/'packed').rglob('transport.json')))
        self.assertTrue(upload(self.lib,'alpha',self.snap['snapshot_id'],packed)['verified'])
        out=self.base/'packed-download'
        download(packed,'alpha',self.snap['snapshot_id'],out)
        restored=Library(self.base/'packed-restore');restored.restore(out)
        self.assertEqual(self.lib.rows('alpha','vault_assets'),restored.rows('alpha','vault_assets'))
        first=next(p for p in (self.base/'packed').rglob('*') if p.is_file() and p.name=='000000')
        first.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum'):
            upload(self.lib,'alpha',self.snap['snapshot_id'],packed)


if __name__=='__main__':
    unittest.main()
