"""Exercise the real Worker routes through a loopback synthetic R2/D1 adapter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import io
import zipfile
from urllib.request import Request, build_opener
from unittest.mock import patch

from outpost.source_restore import Reader, restore
from outpost.source_versions import freeze, publish


@unittest.skipUnless(shutil.which('node'), 'Node is required for Worker integration tests')
class SourceIntegrationTests(unittest.TestCase):
    def test_python_publisher_and_portable_reader_use_actual_worker_routes(self):
        repo = Path(__file__).resolve().parents[1]
        env = {k: v for k, v in os.environ.items() if k not in ('OUTPOST_PREVIEW_UI', 'PORT')}
        process = subprocess.Popen(['node', str(repo / 'viewer/source-preview.mjs')], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        try:
            info = json.loads(process.stdout.readline())
            origin = 'http://127.0.0.1:' + str(info['port'])
            opener = build_opener()
            class Client:
                app = 'demo'
                def request(self, route, data=None, *, raw=None, method=None):
                    body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
                    request = Request(origin + '/api/sources/demo/' + route, data=body,
                                      method=method or ('POST' if body is not None else 'GET'),
                                      headers={'Authorization': 'Bearer private'})
                    with opener.open(request, timeout=20) as response: result = response.read()
                    return result if route.startswith('blob/') and method != 'PUT' else json.loads(result)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                work = root / 'work'; work.mkdir()
                (work / 'scene.py').write_text('# synthetic editable scene\n')
                (work / 'captions.ro.srt').write_text('Bună seara\n')
                revision = freeze(work, {'app_id': 'demo', 'video_id': 'production:integration', 'label': 'Încercare',
                                         'files': [{'path': 'scene.py', 'role': 'source'}, {'path': 'captions.ro.srt', 'role': 'transcript', 'language': 'ro'}]})
                result = publish(Client(), revision, work, 2, 'python-integration')
                self.assertEqual('archived_and_viewer_verified', result['publication_status'])
                self.assertEqual(3, result['revision'])
                self.assertEqual(3, publish(Client(), revision, work, 2, 'python-integration')['revision'])
                with opener.open(Request(origin + '/api/sources/demo/revision/' + revision['revision_id'] + '/package', headers={'Authorization': 'Bearer private'})) as response:
                    package = response.read()
                with zipfile.ZipFile(io.BytesIO(package)) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual({'outpost-source.json', 'scene.py', 'captions.ro.srt'}, set(archive.namelist()))
                    self.assertEqual((work / 'scene.py').read_bytes(), archive.read('scene.py'))
                class LoopbackOpener:
                    def open(self, request, **kwargs):
                        # The production Reader still validates HTTPS and constructs
                        # all paths/cookies; only this test transport targets loopback.
                        translated = Request(request.full_url.replace('https://example.test', origin),
                                             data=request.data, headers=dict(request.headers), method=request.get_method())
                        return opener.open(translated, **kwargs)
                with patch('outpost.source_restore.build_opener', return_value=LoopbackOpener()):
                    reader = Reader('https://example.test', 'read')
                restored = root / 'restored'
                result = restore(revision, restored, lambda file: reader.fetch(revision, file))
                self.assertEqual(2, result['verified_files'])
                self.assertEqual((work / 'captions.ro.srt').read_bytes(), (restored / 'captions.ro.srt').read_bytes())
        finally:
            process.terminate()
            process.communicate(timeout=10)
