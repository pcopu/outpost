import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from outpost.library import Library
from outpost.storage import upload

class PipedStore:
    packed = True
    chunk_size = 4096
    compress_payload = True
    parallel_uploads = True

    def __init__(self, fail=False):
        self.objects = {}
        self.second_started = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.fail = fail
        self.verified = set()

    def get(self, key, destination):
        if key not in self.objects: return False
        Path(destination).write_bytes(self.objects[key])
        return True

    def hash_object(self, key):
        if key not in self.objects: return None
        result = hashlib.sha256(self.objects[key]).hexdigest()
        self.verified.add(key)
        return result

    def put(self, key, path):
        if key.endswith(('transport.json', 'COMMITTED.json')):
            assert self.active == 0, 'Metadata must wait for all payload read-backs'
            assert all(k in self.verified for k in self.objects if '/payload/' in k)
        self.objects[key] = Path(path).read_bytes()

    def put_payload(self, key, data):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if key.endswith('000000'):
                if not self.second_started.wait(2): raise RuntimeError('Second bounded upload did not start')
            if key.endswith('000001'):
                self.second_started.set()
                if self.fail: raise RuntimeError('Synthetic interrupted payload')
            self.objects[key] = data
        finally:
            with self.lock: self.active -= 1

class ParallelUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.lib = Library(Path(self.temp.name)/'library'); self.lib.register('alpha', 'Alpha')
        self.snapshot = self.lib.snapshot('alpha')

    def test_two_bounded_uploads_finish_before_completion_marker(self):
        store = PipedStore()
        result = upload(self.lib, 'alpha', self.snapshot['snapshot_id'], store)
        self.assertTrue(result['verified'])
        self.assertEqual(2, store.peak)
        self.assertTrue(any(k.endswith('/COMMITTED.json') for k in store.objects))

    def test_failed_payload_leaves_no_completion_marker_and_can_resume(self):
        store = PipedStore(fail=True)
        with self.assertRaises(RuntimeError): upload(self.lib, 'alpha', self.snapshot['snapshot_id'], store)
        self.assertFalse(any(k.endswith('/COMMITTED.json') for k in store.objects))
        self.assertEqual(0, store.active)
        store.fail = False
        self.assertTrue(upload(self.lib, 'alpha', self.snapshot['snapshot_id'], store)['verified'])
