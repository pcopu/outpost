import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('outpost.fetch'),
                             'Individual-media retrieval is not implemented')
        from outpost.library import Library
        from outpost.storage import DirectoryStore, upload
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.publisher = Library(self.base / 'publisher')
        self.publisher.register('alpha', 'Alpha')
        self.content = b''.join(hashlib.sha256(str(i).encode()).digest() for i in range(700))
        original = self.base / 'creative.mp4'
        original.write_bytes(self.content)
        self.publisher.import_asset('alpha', original, 'video')
        self.asset = self.publisher.rows('alpha', 'vault_assets')[0]
        self.path = self.asset['stored_path']
        self.snapshot = self.publisher.snapshot('alpha')
        self.ident = self.snapshot['snapshot_id']
        class Tracked(DirectoryStore):
            packed = True
            chunk_size = 4096
            def __init__(self, root):
                super().__init__(root)
                self.reads = []
                self.interrupt = False
            def iter_object(self, key):
                self.reads.append(key)
                for block in super().iter_object(key):
                    if self.interrupt and '/payload/' in key:
                        yield block[:7]
                        raise RuntimeError('interrupted retrieval')
                    yield block
        self.store = Tracked(self.base / 'remote')
        upload(self.publisher, 'alpha', self.ident, self.store)
        self.prefix = f'apps/alpha/snapshots/{self.ident}'
        self.remote = self.store.root / self.prefix
        self.descriptor = json.loads((self.remote / 'transport.json').read_text())
        self.spec = next(x for x in self.descriptor['files'] if x['path'] == self.path)
        size = self.descriptor['chunk_size']
        first = self.spec['offset'] // size
        last = (self.spec['offset'] + len(self.content) - 1) // size
        self.chunks = self.descriptor['chunks'][first:last + 1]
        self.client = Library(self.base / 'client')
        self.client.register('alpha', 'Alpha')
        self.client.register('beta', 'Beta')
        self.out = self.base / 'retrieved.mp4'

    def fetch(self, **kwargs):
        from outpost.fetch import fetch_file
        return fetch_file(self.client, self.store, 'alpha', self.ident,
                          self.path, self.out, **kwargs)

    def test_offloaded_file_fetches_only_intersecting_chunks_without_local_source(self):
        before = (self.client.app_dir('alpha') / 'catalog.sqlite3').read_bytes()
        result = self.fetch()
        self.assertEqual(self.content, self.out.read_bytes())
        self.assertEqual(before, (self.client.app_dir('alpha') / 'catalog.sqlite3').read_bytes())
        self.assertFalse((self.client.app_dir('alpha') / self.path).exists())
        wanted = [self.prefix + '/' + x['path'] for x in self.chunks]
        self.assertEqual(wanted, [x for x in self.store.reads if '/payload/' in x])
        self.assertLess(len(wanted), len(self.descriptor['chunks']))
        self.assertEqual(len(self.content), result['materialized_bytes'])
        self.assertEqual(sum(x['size'] for x in self.chunks), result['remote_payload_bytes'])
        self.assertEqual(result['remote_payload_bytes'] + result['remote_metadata_bytes'],
                         result['remote_bytes_fetched'])
        self.assertTrue(result['verified'])
        self.assertEqual('selected_file_only', result.get('verification_scope'))
        self.assertEqual(0o600, self.out.stat().st_mode & 0o777)

    def test_corrupt_chunk_never_publishes_partial_file(self):
        p = self.remote / self.chunks[-1]['path']
        data = bytearray(p.read_bytes()); data[-1] ^= 1; p.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'checksum|compressed'):
            self.fetch()
        self.assertFalse(self.out.exists())
        self.assertEqual([], list(self.base.glob('.outpost-fetch-*')))

    def test_unrelated_corrupt_chunk_is_not_read_for_an_individual_fetch(self):
        wanted = {x['path'] for x in self.chunks}
        other = next(x for x in self.descriptor['chunks'] if x['path'] not in wanted)
        (self.remote / other['path']).write_bytes(b'bad unrelated content')
        self.assertTrue(self.fetch()['verified'])
        self.assertNotIn(self.prefix + '/' + other['path'], self.store.reads)

    def test_compressed_chunk_cannot_expand_beyond_its_declared_bound(self):
        import gzip
        chunk = self.chunks[0]
        body = gzip.compress(b'x' * (2 * 1024 * 1024), mtime=0)
        (self.remote / chunk['path']).write_bytes(body)
        td = self.remote / 'transport.json'
        data = json.loads(td.read_text())
        item = next(x for x in data['chunks'] if x['path'] == chunk['path'])
        item.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
        td.write_text(json.dumps(data))
        marker = self.remote / 'COMMITTED.json'
        commit = json.loads(marker.read_text()); commit['transport_sha256'] = hashlib.sha256(td.read_bytes()).hexdigest()
        marker.write_text(json.dumps(commit))
        with self.assertRaisesRegex(ValueError, 'declared size'):
            self.fetch()
        self.assertFalse(self.out.exists())

    def test_manifest_symlink_is_not_followed_to_a_different_path(self):
        from outpost.fetch import fetch_file
        from outpost.storage import upload
        files = self.publisher.app_dir('alpha') / 'files'
        alias = files / 'alias'; alias.symlink_to(os.path.relpath(files.parent / self.path, files))
        snap = self.publisher.snapshot('alpha')
        upload(self.publisher, 'alpha', snap['snapshot_id'], self.store)
        with self.assertRaisesRegex(ValueError, 'regular backed-up file'):
            fetch_file(self.client, self.store, 'alpha', snap['snapshot_id'], 'files/alias', self.out)
        self.assertFalse(self.out.exists())

    def test_interrupted_fetch_cleans_only_own_scratch_and_can_be_retried(self):
        sentinel = self.base / '.outpost-fetch-unrelated'; sentinel.write_bytes(b'keep')
        self.store.interrupt = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted retrieval'):
            self.fetch()
        self.assertFalse(self.out.exists())
        self.assertEqual([sentinel], list(self.base.glob('.outpost-fetch-*')))
        self.store.interrupt = False
        self.assertTrue(self.fetch()['verified'])
        self.assertEqual(b'keep', sentinel.read_bytes())

    def test_wrong_app_and_traversal_cannot_read_another_app(self):
        from outpost.fetch import fetch_file
        for app, path in [('../alpha', self.path), ('alpha', '../catalog.sqlite3'),
                          ('alpha', 'files/../../escape'), ('alpha', '/files/absolute'),
                          ('alpha', 'catalog.sqlite3')]:
            with self.subTest(app=app, path=path):
                with self.assertRaises(ValueError):
                    fetch_file(self.client, self.store, app, self.ident, path, self.out)
        self.assertEqual([], self.store.reads)
        target = self.store.root / 'apps/beta/snapshots' / self.ident
        target.mkdir(parents=True)
        shutil.copyfile(self.remote / 'COMMITTED.json', target / 'COMMITTED.json')
        with self.assertRaisesRegex(ValueError, 'identity'):
            fetch_file(self.client, self.store, 'beta', self.ident, self.path, self.out)
        self.assertFalse(self.out.exists())

    def test_symlink_destination_and_managed_library_destination_are_rejected(self):
        from outpost.fetch import fetch_file
        outside = self.base / 'outside'; outside.mkdir()
        alias = self.base / 'alias'; alias.symlink_to(outside, target_is_directory=True)
        for out in [alias / 'result', self.client.app_dir('beta') / 'unrelated',
                    self.base / '..' / 'escape-fetch']:
            with self.subTest(out=out):
                with self.assertRaises(ValueError):
                    fetch_file(self.client, self.store, 'alpha', self.ident, self.path, out)
        self.assertEqual([], self.store.reads)
        self.assertEqual([], list(outside.iterdir()))

    def test_existing_destination_is_never_overwritten_or_followed(self):
        self.out.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            self.fetch()
        self.assertEqual(b'existing', self.out.read_bytes())
        self.out.unlink(); other = self.base / 'other'; other.write_bytes(b'unchanged')
        self.out.symlink_to(other)
        with self.assertRaises((ValueError, FileExistsError)):
            self.fetch()
        self.assertEqual(b'unchanged', other.read_bytes())
        self.assertEqual([], self.store.reads)

    def test_dry_run_reads_only_verified_metadata_and_does_not_create_output(self):
        before = sorted(str(p) for p in self.base.rglob('*'))
        result = self.fetch(dry_run=True)
        self.assertEqual(before, sorted(str(p) for p in self.base.rglob('*')))
        self.assertFalse(any('/payload/' in key for key in self.store.reads))
        self.assertEqual(len(self.chunks), result['planned_chunks'])
        self.assertEqual(sum(x['size'] for x in self.chunks), result['planned_payload_bytes'])
        self.assertEqual(0, result['materialized_bytes'])
        self.assertFalse(self.out.exists())

    def test_missing_remote_object_is_actionable_and_atomic(self):
        (self.remote / self.chunks[0]['path']).unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'Remote object'):
            self.fetch()
        self.assertFalse(self.out.exists())
        self.assertEqual([], list(self.base.glob('.outpost-fetch-*')))

    def test_transport_mapping_cannot_substitute_a_different_file_range(self):
        p = self.remote / 'transport.json'
        data = json.loads(p.read_text())
        next(x for x in data['files'] if x['path'] == self.path)['offset'] += 1
        p.write_text(json.dumps(data))
        marker = self.remote / 'COMMITTED.json'
        commit = json.loads(marker.read_text()); commit['transport_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        marker.write_text(json.dumps(commit))
        with self.assertRaisesRegex(ValueError, 'Transport'):
            self.fetch()
        self.assertFalse(self.out.exists())

    def test_unpacked_backend_streams_only_the_requested_file(self):
        from outpost.storage import DirectoryStore, upload
        class Unpacked(type(self.store)):
            packed = False
        self.store = Unpacked(self.base / 'unpacked')
        upload(self.publisher, 'alpha', self.ident, self.store)
        result = self.fetch()
        self.assertEqual(self.content, self.out.read_bytes())
        self.assertEqual(len(self.content), result['remote_payload_bytes'])
        self.assertEqual(0, result['chunks_fetched'])
        self.assertEqual([self.prefix + '/' + self.path],
                         [p for p in self.store.reads if '/files/' in p])

    def test_unpacked_same_size_corruption_is_rejected_by_file_hash(self):
        from outpost.storage import upload
        class Unpacked(type(self.store)):
            packed = False
        self.store = Unpacked(self.base / 'unpacked-corrupt')
        upload(self.publisher, 'alpha', self.ident, self.store)
        (self.store.root / self.prefix / self.path).write_bytes(b'x' * len(self.content))
        with self.assertRaisesRegex(ValueError, 'file checksum'):
            self.fetch()
        self.assertFalse(self.out.exists())

    def test_legacy_uncompressed_chunks_and_empty_files_are_supported(self):
        from outpost.storage import upload
        self.store.compress_payload = False
        self.store.root = self.base / 'legacy'
        upload(self.publisher, 'alpha', self.ident, self.store)
        self.assertEqual(self.content, self.fetch() and self.out.read_bytes())
        original = self.base / 'empty'; original.write_bytes(b'')
        self.publisher.import_asset('alpha', original)
        row = next(x for x in self.publisher.rows('alpha', 'vault_assets') if x['file_bytes'] == 0)
        snap = self.publisher.snapshot('alpha')
        upload(self.publisher, 'alpha', snap['snapshot_id'], self.store)
        from outpost.fetch import fetch_file
        result = fetch_file(self.client, self.store, 'alpha', snap['snapshot_id'],
                            row['stored_path'], self.base / 'empty-output')
        self.assertEqual(0, result['remote_payload_bytes'])
        self.assertEqual(b'', (self.base / 'empty-output').read_bytes())


if __name__ == '__main__':
    unittest.main()
