import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
try:
    from outpost.gateway import WorkerStore, NoRedirect
except ImportError:
    WorkerStore = NoRedirect = None
from outpost.library import Library
from outpost.storage import upload, verify_remote, download

class WorkerStorageTests(unittest.TestCase):
    def test_authenticated_archive_roundtrip_and_immutable_retries(self):
        self.assertIsNotNone(WorkerStore, 'Worker storage backend is implemented')
        class Endpoint:
            def __init__(self): self.objects = {}; self.calls = []
            def open(self, request, timeout):
                self.calls.append(request)
                assert request.get_header('Authorization') == 'Bearer synthetic-admin-token'
                assert request.full_url.startswith('https://archive.example/api/archive/alpha/snapshots/')
                key = request.full_url
                if request.get_method() == 'PUT':
                    body = request.data
                    assert hashlib.sha256(body).hexdigest() == request.get_header('X-outpost-sha256')
                    if key in self.objects: raise HTTPError(key, 409, 'exists', {}, None)
                    self.objects[key] = body
                    return io.BytesIO(b'{}')
                if key not in self.objects: raise HTTPError(key, 404, 'missing', {}, None)
                return io.BytesIO(self.objects[key])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'OUTPOST_ARCHIVE_TOKEN': 'synthetic-admin-token'}):
            base = Path(temp); lib = Library(base/'library'); lib.register('alpha', 'Alpha')
            source = base/'film.mp4'; source.write_bytes(b'synthetic render')
            lib.import_asset('alpha', source); snapshot = lib.snapshot('alpha')
            store = WorkerStore('https://archive.example'); store.chunk_size = 4096; store._opener = Endpoint()
            self.assertTrue(upload(lib, 'alpha', snapshot['snapshot_id'], store)['verified'])
            puts = sum(r.get_method() == 'PUT' for r in store._opener.calls)
            self.assertTrue(upload(lib, 'alpha', snapshot['snapshot_id'], store)['verified'])
            self.assertEqual(puts, sum(r.get_method() == 'PUT' for r in store._opener.calls))
            self.assertTrue(verify_remote(store, 'alpha', snapshot['snapshot_id'])['verified'])
            self.assertTrue(download(store, 'alpha', snapshot['snapshot_id'], base/'download')['verified'])
            with self.assertRaises(ValueError): store.get('viewer/index.json', base/'unsafe')
            self.assertFalse((base/'unsafe').exists())

    def test_configuration_keeps_credentials_in_environment_and_blocks_redirects(self):
        self.assertIsNotNone(WorkerStore, 'Worker storage backend is implemented')
        from outpost.configuration import validate, backend
        config = {'format_version': 1, 'backend': 'worker', 'url': 'https://archive.example', 'token_env': 'OUTPOST_ARCHIVE_TOKEN', 'private_confirmed': True}
        self.assertEqual(config, validate(config))
        from outpost.cli import parser, reference
        args = parser().parse_args(['storage', 'configure', '--worker-url', 'https://archive.example', '--private-confirmed'])
        self.assertEqual(config, reference(args))
        self.assertIsInstance(backend(config), WorkerStore)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other.example'))
        for url in ['http://archive.example', 'https://user:password@archive.example', 'https://archive.example?key=secret', 'https://archive.example/path']:
            with self.subTest(url=url), self.assertRaises(ValueError): WorkerStore(url)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            list(WorkerStore('https://archive.example').iter_object('apps/alpha/snapshots/'+'a'*64+'/manifest.json'))
