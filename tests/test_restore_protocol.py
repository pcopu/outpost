"""Reject unsupported remote completion protocols before reading snapshot data."""
import json
from pathlib import Path
import tempfile
import unittest

from outpost.fetch import fetch_file
from outpost.library import Library
from outpost.storage import DirectoryStore, download, upload, verify_remote


class RestoreProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.lib = Library(self.base / 'library')
        self.lib.register('alpha', 'Alpha')
        source = self.base / 'clip.mp4'
        source.write_bytes(b'synthetic creative')
        self.lib.import_asset('alpha', source)
        self.asset = self.lib.rows('alpha', 'vault_assets')[0]['stored_path']
        self.snapshot = self.lib.snapshot('alpha')
        self.ident = self.snapshot['snapshot_id']

        class Tracked(DirectoryStore):
            def __init__(self, root):
                super().__init__(root)
                self.reads = []

            def get(self, key, destination):
                self.reads.append(key)
                return super().get(key, destination)

            def iter_object(self, key):
                self.reads.append(key)
                yield from super().iter_object(key)

        self.store = Tracked(self.base / 'objects')
        upload(self.lib, 'alpha', self.ident, self.store)
        self.prefix = f'apps/alpha/snapshots/{self.ident}'
        self.marker_path = self.store.root / self.prefix / 'COMMITTED.json'
        self.marker = json.loads(self.marker_path.read_text())
        self.out = self.base / 'result'
        self.attempt = 0
        self.operations = {
            'download': lambda: download(self.store, 'alpha', self.ident, self.out),
            'verify': lambda: verify_remote(self.store, 'alpha', self.ident),
            'fetch': lambda: fetch_file(self.lib, self.store, 'alpha', self.ident,
                                       self.asset, self.out),
        }

    def assert_rejected_before_snapshot_read(self, marker):
        self.marker_path.write_text(json.dumps(marker))
        for operation, run in self.operations.items():
            with self.subTest(operation=operation):
                self.attempt += 1
                self.out = self.base / f'result-{self.attempt}'
                self.store.reads.clear()
                with self.assertRaises(ValueError):
                    run()
                self.assertEqual([self.prefix + '/COMMITTED.json'], self.store.reads)
                self.assertFalse(self.out.exists())
                self.assertEqual([], list(self.base.glob('.outpost-fetch-*')))

    def test_unknown_completion_version_is_rejected(self):
        self.assert_rejected_before_snapshot_read(dict(self.marker, format_version=2))

    def test_missing_completion_version_is_rejected(self):
        marker = dict(self.marker)
        marker.pop('format_version')
        self.assert_rejected_before_snapshot_read(marker)

    def test_noninteger_completion_versions_are_rejected(self):
        for version in (True, 1.0, '1', None):
            with self.subTest(version=version):
                self.assert_rejected_before_snapshot_read(dict(self.marker, format_version=version))

    def test_unknown_transport_cannot_fall_back_to_unpacked_objects(self):
        self.assert_rejected_before_snapshot_read(dict(self.marker, transport='packed-v2'))

    def test_nonstring_and_empty_transports_are_rejected(self):
        for transport in (False, 0, '', [], {}):
            with self.subTest(transport=transport):
                self.assert_rejected_before_snapshot_read(dict(self.marker, transport=transport))

    def test_nonobject_completion_markers_fail_visibly(self):
        for marker in (None, [], 'not a completion marker', 1):
            with self.subTest(marker=marker):
                self.assert_rejected_before_snapshot_read(marker)

    def test_manifest_digest_must_be_a_full_sha256(self):
        for value in (None, True, 'not-a-sha256', 'A' * 64):
            with self.subTest(digest=value):
                self.assert_rejected_before_snapshot_read(dict(self.marker, manifest_sha256=value))

    def test_packed_transport_requires_a_full_descriptor_digest(self):
        marker = dict(self.marker, transport='packed-v1')
        self.assert_rejected_before_snapshot_read(marker)
        for value in (None, [], 'bad', 'A' * 64):
            with self.subTest(digest=value):
                self.assert_rejected_before_snapshot_read(dict(marker, transport_sha256=value))

    def test_identity_mismatch_stops_before_reading_snapshot(self):
        for changed in ({'app_id': 'beta'}, {'snapshot_id': '0' * 64}):
            with self.subTest(changed=changed):
                self.assert_rejected_before_snapshot_read(dict(self.marker, **changed))

    def test_packed_remote_verification_rejects_unknown_completion_version(self):
        class Packed(type(self.store)):
            packed = True
            chunk_size = 4096
        self.store = Packed(self.base / 'unsupported-version-packed')
        upload(self.lib, 'alpha', self.ident, self.store)
        self.marker_path = self.store.root / self.prefix / 'COMMITTED.json'
        marker = json.loads(self.marker_path.read_text())
        self.assert_rejected_before_snapshot_read(dict(marker, format_version=2))

    def test_valid_unpacked_protocol_still_restores_and_fetches(self):
        self.assertTrue(download(self.store, 'alpha', self.ident, self.out)['verified'])
        fetched = self.base / 'fetched.mp4'
        result = fetch_file(self.lib, self.store, 'alpha', self.ident, self.asset, fetched)
        self.assertTrue(result['verified'])
        self.assertEqual(b'synthetic creative', fetched.read_bytes())
        with self.assertRaisesRegex(ValueError, 'requires a packed snapshot'):
            verify_remote(self.store, 'alpha', self.ident)

    def test_both_existing_packed_encodings_remain_compatible(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                class Packed(DirectoryStore):
                    packed = True
                    chunk_size = 4096
                    compress_payload = compressed
                store = Packed(self.base / f'packed-{compressed}')
                upload(self.lib, 'alpha', self.ident, store)
                self.assertTrue(verify_remote(store, 'alpha', self.ident)['verified'])
                restored = self.base / f'restored-{compressed}'
                self.assertTrue(download(store, 'alpha', self.ident, restored)['verified'])
                fetched = self.base / f'fetched-{compressed}'
                self.assertTrue(fetch_file(self.lib, store, 'alpha', self.ident,
                                           self.asset, fetched)['verified'])
                self.assertEqual(b'synthetic creative', fetched.read_bytes())


if __name__ == '__main__':
    unittest.main()
