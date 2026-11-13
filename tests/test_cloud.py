import hashlib
import importlib.util
import json
import sqlite3
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from outpost.library import Library


class CloudTests(unittest.TestCase):
    def cloud(self):
        self.assertIsNotNone(importlib.util.find_spec('outpost.cloud'),
                             'Cloud catalogs must have a lossless remote client')
        from outpost import cloud
        return cloud

    def test_large_values_and_literal_reference_strings_round_trip(self):
        cloud = self.cloud()
        objects = {}
        def put(data):
            sha = hashlib.sha256(data).hexdigest()
            objects[sha] = data
            return sha
        for value in ['é' * 700000, b'\x00\xff' * 600000,
                      cloud.REFERENCE_PREFIX + 'literal-user-data', 'small', None, 1.25]:
            encoded = cloud.encode_value(value, put)
            self.assertEqual(value, cloud.decode_value(encoded, objects.__getitem__))
        self.assertEqual(3, len(objects))

    def test_blob_corruption_fails_instead_of_returning_wrong_data(self):
        cloud = self.cloud()
        encoded = cloud.encode_value('x' * 1100000, lambda b: hashlib.sha256(b).hexdigest())
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cloud.decode_value(encoded, lambda sha: b'bad')

    def test_numeric_precision_and_oversized_rows_fail_closed(self):
        cloud = self.cloud()
        with self.assertRaisesRegex(ValueError, 'integer'):
            cloud.encode_value(2 ** 53, lambda b: None)
        objects = {}
        def put(data):
            sha = hashlib.sha256(data).hexdigest(); objects[sha] = data; return sha
        original = {'a': 'a' * 900000, 'b': 'b' * 900000, 'id': 'stable'}
        encoded = cloud.encode_row(original, put)
        self.assertLess(len(json.dumps(encoded)), 1500000)
        self.assertEqual(original, {k: cloud.decode_value(v, objects.__getitem__) for k, v in encoded.items()})

    def test_cloud_catalog_queries_do_not_recreate_local_database(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as tmp:
            lib = Library(Path(tmp).resolve()); lib.register('demo', 'Demo')
            appdir = lib.app_dir('demo'); (appdir / 'catalog.sqlite3').unlink()
            (appdir / 'cloud.json').write_text(json.dumps({
                'format_version': 1, 'backend': 'd1', 'app_id': 'demo',
                'worker_url': 'https://example.test', 'database_id': 'db-demo',
                'token_env': 'OUTPOST_ARCHIVE_TOKEN'}))
            expected = [{'handle': 'demo', 'kind': 'brand'}]
            with patch.object(cloud.CloudCatalog, 'rows', return_value=expected) as query:
                self.assertEqual(expected, lib.rows('demo', 'accounts', {'kind': 'brand'}, 10))
                query.assert_called_once_with('accounts', {'kind': 'brand'}, 10)
            self.assertFalse((appdir / 'catalog.sqlite3').exists())

    def test_cloud_reference_rejects_cross_app_and_credential_urls(self):
        cloud = self.cloud()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); lib = Library(root); lib.register('demo', 'Demo')
            p = lib.app_dir('demo') / 'cloud.json'
            base = {'format_version': 1, 'backend': 'd1', 'app_id': 'other',
                    'worker_url': 'https://example.test', 'database_id': 'db-demo'}
            p.write_text(json.dumps(base))
            with self.assertRaises(ValueError): cloud.for_app(lib, 'demo')
            p.write_text(json.dumps(dict(base, app_id='demo', worker_url='https://secret@example.test')))
            with self.assertRaises(ValueError): cloud.for_app(lib, 'demo')

    def test_remote_rows_paginate_and_reject_concurrent_changes(self):
        cloud = self.cloud()
        client = cloud.CloudCatalog({'format_version':1,'backend':'d1','app_id':'demo',
                                     'worker_url':'https://example.test','database_id':'db'})
        self.assertTrue(hasattr(client, 'request'), 'Cloud transport must support bounded requests')
        def respond(action, data=None, **kwargs):
            if action == 'state': return {'revision':2}
            offset=data['offset'];limit=data['limit']
            return {'rows':[{'id':i} for i in range(offset,min(offset+limit,105))],'revision':2}
        with patch.object(client,'request',side_effect=respond):
            self.assertEqual(105,len(client.rows('records',limit=None)))
            self.assertEqual(3,len(client.rows('records',limit=3)))
        with patch.object(client,'request',side_effect=[{'rows':[{'id':1}],'revision':2},{'revision':3}]):
            with self.assertRaisesRegex(RuntimeError,'changed'): client.rows('records',limit=1)

    def test_remote_record_dry_run_does_not_upload_large_values(self):
        cloud = self.cloud()
        client=cloud.CloudCatalog({'format_version':1,'backend':'d1','app_id':'demo',
                                  'worker_url':'https://example.test','database_id':'db'})
        self.assertTrue(hasattr(client,'record'), 'Remote records must support dry runs')
        schema={'schema':[{'name':'accounts','type':'table','columns':[
            {'name':'handle','pk':1},{'name':'kind','pk':0},{'name':'display_name','pk':0}]}]}
        def respond(action,data=None,**kwargs):
            if action=='schema':return schema
            if action=='state':return {'revision':7}
            if action=='mutate':return {'dry_run':data['dry_run'],'revision':7}
            self.fail('Unexpected remote operation: '+action)
        with patch.object(client,'request',side_effect=respond),patch.object(client,'upload_blob') as upload:
            result=client.record('accounts',{'handle':'one','kind':'brand','display_name':'x'*1100000},True)
            self.assertTrue(result['dry_run']);upload.assert_not_called()

    def test_remote_transaction_rejects_credentials_before_upload(self):
        cloud=self.cloud()
        client=cloud.CloudCatalog({'format_version':1,'backend':'d1','app_id':'demo',
                                  'worker_url':'https://example.test','database_id':'db'})
        self.assertTrue(hasattr(client,'mutate'), 'Cloud writes must preserve secret screening')
        with patch.object(client,'request') as request,patch.object(client,'upload_blob') as upload:
            with self.assertRaisesRegex(ValueError,'credential'):
                client.mutate([{'sql':'INSERT INTO accounts VALUES (?)','params':[json.dumps({'api_key':'x'*40})]}],0,'test')
            request.assert_not_called();upload.assert_not_called()

    def test_library_record_routes_cloud_and_rejects_unarchived_paths(self):
        cloud=self.cloud()
        with tempfile.TemporaryDirectory() as temp:
            lib=Library(Path(temp).resolve());lib.register('demo','Demo');appdir=lib.app_dir('demo')
            (appdir/'cloud.json').write_text(json.dumps({'format_version':1,'backend':'d1','app_id':'demo',
                'worker_url':'https://example.test','database_id':'db'}))
            (appdir/'catalog.sqlite3').unlink()
            with patch.object(cloud.CloudCatalog,'record',return_value={'revision':1}) as record, patch.object(cloud.CloudCatalog,'rows',return_value=[]):
                try: result=lib.record('demo','accounts',{'handle':'one','kind':'brand'})
                except sqlite3.Error: self.fail('Cloud record still depends on local SQLite')
                self.assertEqual({'revision':1},result)
                record.assert_called_once()
                with self.assertRaisesRegex(ValueError,'archiv'):
                    lib.record('demo','videos',{'video_id':'one','video_path':'files/not-archived'})

    def test_cloud_cli_status_and_lineage_work_without_local_database(self):
        cloud=self.cloud()
        from outpost.cli import parser,execute
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();lib=Library(root);lib.register('demo','Demo');appdir=lib.app_dir('demo')
            (appdir/'cloud.json').write_text(json.dumps({'format_version':1,'backend':'d1','app_id':'demo',
                'worker_url':'https://example.test','database_id':'db'}))
            (appdir/'catalog.sqlite3').unlink()
            with patch.object(cloud.CloudCatalog,'request',return_value={'phase':'ready','revision':3}):
                try: args=parser().parse_args(['--root',str(root),'cloud','status','demo'])
                except SystemExit: self.fail('Cloud status command is missing')
                self.assertEqual('ready',execute(args)['cloud']['phase'])
            with patch.object(cloud.CloudCatalog,'sql',return_value={'rows':[{'from_key':'one','to_key':'two'}],'revision':3}):
                args=parser().parse_args(['--root',str(root),'lineage','demo','one'])
                self.assertEqual('two',execute(args)[0]['to_key'])

    def test_local_only_operations_explain_cloud_mode_without_creating_a_database(self):
        from outpost.util import connect
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'catalog.sqlite3'
            (path.parent/'cloud.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'cloud'):
                connect(path)
            self.assertFalse(path.exists())

    def test_connection_reset_retries_the_identical_idempotent_payload(self):
        cloud=self.cloud()
        client=cloud.CloudCatalog({'format_version':1,'backend':'d1','app_id':'demo',
            'worker_url':'https://example.test','database_id':'db'})
        calls=[]
        class Endpoint:
            def open(self,request,timeout):
                calls.append(request.data)
                if len(calls)<3:raise ConnectionResetError('synthetic reset')
                return io.BytesIO(b'{"replayed":true}')
        with patch.object(client,'token',return_value='synthetic'),patch.object(cloud,'build_opener',return_value=Endpoint()),patch.object(cloud.time,'sleep'):
            self.assertTrue(client.request('import',{'batch_id':'stable','rows':[[1,'value']]})['replayed'])
        self.assertEqual(3,len(calls));self.assertEqual([calls[0]]*3,calls)


if __name__ == '__main__':
    unittest.main()
