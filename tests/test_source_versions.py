import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class SourceVersionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('outpost.source_versions'), 'Exact source revisions are not implemented')
        from outpost import source_versions as sources
        from outpost import source_restore as restore
        self.sources, self.restore = sources, restore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'scene.py').write_text('# editable scene\n')
        (self.work / 'voice.wav').write_bytes(b'original narration')
        self.spec = {'app_id': 'demo', 'video_id': 'production:film', 'label': 'First cut',
                     'files': [{'path': 'scene.py', 'role': 'source'},
                               {'path': 'voice.wav', 'role': 'audio', 'language': 'en'}],
                     'dependencies': [{'name': 'narration', 'status': 'present', 'path': 'voice.wav'},
                                      {'name': 'font', 'status': 'missing'}],
                     'recipe': {'entrypoint': 'scene.py', 'command': ['python', 'scene.py'], 'tools': {'python': '3.12'}},
                     'lineage': []}

    def freeze(self):
        return self.sources.freeze(self.work, self.spec)

    def test_content_revision_ignores_label_and_pins_actual_files(self):
        first = self.freeze()
        self.spec['label'] = 'Renamed'
        self.assertEqual(first['revision_id'], self.freeze()['revision_id'])
        (self.work / 'scene.py').write_text('# changed\n')
        second = self.freeze()
        self.assertNotEqual(first['revision_id'], second['revision_id'])
        self.assertEqual('unknown', first['manifest']['git']['working_tree'])
        self.assertEqual('missing', first['manifest']['dependencies'][1]['status'])

    def test_git_revision_truthfully_records_dirty_and_untracked_state(self):
        for args in [['init', '-q'], ['add', 'scene.py'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'Synthetic']]:
            subprocess.run(['git', '-C', str(self.work), *args], check=True, capture_output=True)
        revision = self.freeze()
        self.assertEqual('dirty', revision['manifest']['git']['working_tree'])
        self.assertEqual(40, len(revision['manifest']['git']['commit']))
        self.assertTrue(revision['manifest']['git']['source_tree_sha256'])

    def test_unsafe_paths_collisions_symlinks_and_secrets_fail(self):
        for name in ['../escape', '/absolute', 'CON.txt', 'a:stream', 'trailing.', 'dir/../a', 'a\\b', '.env', 'outpost-source.json']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.restore.safe_path(name)
        for names in [['scene.py', 'SCENE.py'], ['dir', 'dir/file']]:
            with self.assertRaises(ValueError): self.restore.validate_paths(names)
        (self.work / 'link').symlink_to(self.work / 'scene.py')
        self.spec['files'].append({'path': 'link', 'role': 'source'})
        with self.assertRaises(ValueError): self.freeze()

    def test_restore_is_version_specific_resumable_and_refuses_changes(self):
        revision = self.freeze()
        data = {f['path']: (self.work / f['path']).read_bytes() for f in revision['manifest']['files']}
        calls = []
        def fetch(file):
            calls.append(file['path'])
            yield data[file['path']]
        out = self.root / 'restored'
        result = self.restore.restore(revision, out, fetch)
        self.assertEqual(2, result['verified_files'])
        self.assertEqual(b'original narration', (out / 'voice.wav').read_bytes())
        self.assertFalse(result['render_tested'])
        calls.clear()
        self.restore.restore(revision, out, fetch, resume=True)
        self.assertEqual([], calls)
        (out / 'voice.wav').write_bytes(b'local edit')
        with self.assertRaises(FileExistsError): self.restore.restore(revision, out, fetch, resume=True)
        self.assertEqual(b'local edit', (out / 'voice.wav').read_bytes())

    def test_corruption_interruption_and_missing_only_checkout(self):
        revision = self.freeze()
        out = self.root / 'restore'
        with self.assertRaises(ValueError): self.restore.restore(revision, out, lambda file: iter([b'corrupt']))
        self.assertFalse((out / 'scene.py').exists())
        checkout = self.root / 'checkout'
        checkout.mkdir()
        (checkout / 'scene.py').write_bytes((self.work / 'scene.py').read_bytes())
        calls = []
        def fetch(file):
            calls.append(file['path'])
            yield (self.work / file['path']).read_bytes()
        self.restore.restore(revision, checkout, fetch, missing_only=True)
        self.assertEqual(['voice.wav'], calls)
        altered = copy.deepcopy(revision)
        altered['manifest']['app_id'] = 'other'
        with self.assertRaises(ValueError): self.restore.restore(altered, self.root / 'bad', fetch)

    def test_present_dependencies_must_pin_files(self):
        self.spec['dependencies'][0]['path'] = 'missing.wav'
        with self.assertRaises(ValueError): self.freeze()

    def test_source_tree_hash_must_match_selected_files(self):
        revision = self.freeze()
        revision['manifest']['git']['source_tree_sha256'] = 'a' * 64
        revision['revision_id'] = self.restore.sha(self.restore.canonical(revision['manifest']))
        with self.assertRaises(ValueError): self.restore.validate(revision)

    def test_publication_never_commits_changed_frozen_inputs(self):
        revision = self.freeze()
        (self.work / 'voice.wav').write_bytes(b'changed after freeze')
        class Client:
            def request(self, *args, **kwargs): raise AssertionError('No remote mutation before local validation')
        with self.assertRaises(ValueError):
            self.sources.publish(Client(), revision, self.work, 0, 'freeze-one')

    def test_publication_upload_readback_retry_and_pending_viewer(self):
        revision = self.freeze()
        class Client:
            app = 'demo'
            def __init__(self): self.blobs, self.commits, self.fail_view, self.uploads = {}, [], False, 0
            def request(self, action, data=None, *, raw=None, method=None):
                if action.startswith('blob/'):
                    if method == 'PUT': self.blobs[action] = raw; self.uploads += 1; return {}
                    if action not in self.blobs: raise FileNotFoundError(action)
                    return self.blobs[action]
                if action == 'publish': self.commits.append(data); return {'revision': 1}
                if action.startswith('confirm/'): return {}
                if self.fail_view: raise RuntimeError('offline')
                return revision
        client = Client()
        result = self.sources.publish(client, revision, self.work, 0, 'one')
        self.assertEqual('archived_and_viewer_verified', result['publication_status'])
        client.fail_view = True
        result = self.sources.publish(client, revision, self.work, 0, 'one')
        self.assertEqual('archived_viewer_verification_pending', result['publication_status'])
        self.assertEqual(client.commits[0], client.commits[1])
        self.assertEqual(2, client.uploads, 'A retry must read back existing chunks without uploading them again')

    def test_cli_freezes_sources_without_upload_or_execution(self):
        from outpost.cli import main
        import contextlib, io
        spec = self.root / 'spec.json'
        spec.write_text(json.dumps(self.spec))
        out = self.root / 'frozen.json'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try: code = main(['source', 'freeze', 'demo', str(self.work), '--spec', str(spec), '--out', str(out)])
            except SystemExit: self.fail('Source CLI not exposed')
        self.assertEqual(0, code)
        self.assertEqual(self.freeze()['revision_id'], json.loads(out.read_text())['revision_id'])

    def test_decimal_metadata_requires_explicit_string_for_cross_runtime_hashes(self):
        self.spec['recipe']['tools']['fps'] = 30.0
        with self.assertRaises(ValueError): self.freeze()

    def test_numeric_object_keys_are_not_canonical_across_runtimes(self):
        self.spec['recipe']['tools'] = {'10': 'first', '2': 'second'}
        with self.assertRaises(ValueError): self.freeze()

    def test_portable_reader_uses_the_real_worker_route(self):
        from io import BytesIO
        from outpost.source_restore import Reader
        revision = self.freeze()
        file = revision['manifest']['files'][0]
        reader = object.__new__(Reader)
        reader.origin, reader.cookie = 'https://example.test', 'session=synthetic'
        class Opener:
            def open(self, request, **kwargs):
                self.url = request.full_url
                return BytesIO(b'bytes')
        reader.opener = Opener()
        self.assertEqual([b'bytes'], list(reader.fetch(revision, file)))
        self.assertEqual('https://example.test/api/sources/demo/revision/' + revision['revision_id'] + '/files/0', reader.opener.url)

    def test_interrupted_restore_resumes_only_missing_files(self):
        revision = self.freeze()
        out = self.root / 'interrupted'
        calls = []
        def interrupted(file):
            if file['path'] == 'voice.wav': raise ConnectionError('interrupted')
            yield (self.work / file['path']).read_bytes()
        with self.assertRaises(ConnectionError): self.restore.restore(revision, out, interrupted)
        self.assertTrue((out / 'scene.py').exists())
        self.assertFalse((out / 'voice.wav').exists())
        def resumed(file):
            calls.append(file['path']); yield (self.work / file['path']).read_bytes()
        self.restore.restore(revision, out, resumed, resume=True)
        self.assertEqual(['voice.wav'], calls)

    def test_restore_rejects_symlink_parent_and_dry_run_does_not_create_files(self):
        revision = self.freeze()
        out = self.root / 'new'
        self.restore.restore(revision, out, lambda file: (), dry_run=True)
        self.assertFalse(out.exists())
        out.symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(ValueError): self.restore.restore(revision, out, lambda file: (), missing_only=True)
