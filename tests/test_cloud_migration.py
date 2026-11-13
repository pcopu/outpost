import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from outpost.library import Library
from outpost.offload import offload
from outpost.storage import DirectoryStore


class MigrationTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('outpost.cloud_migration'),
                             'Catalog migration must verify all rows before removing local SQLite')
        from outpost import cloud_migration
        return cloud_migration

    def fixture(self, root):
        lib=Library(root/'library');lib.register('demo','Demo')
        lib.record('demo','accounts',{'handle':'one','kind':'brand','display_name':'é'*700000})
        db=sqlite3.connect(lib.app_dir('demo')/'catalog.sqlite3')
        db.execute("INSERT INTO source_entities(entity_key,kind,parent_key,metadata_json) VALUES('child','item','parent','{}')")
        db.execute("INSERT INTO source_entities(entity_key,kind,metadata_json) VALUES('parent','item','{}')")
        db.commit();db.close()
        store=DirectoryStore(root/'archive');store.packed=True
        offload(lib,'demo',store)
        return lib

    def test_dry_run_has_no_cloud_calls_and_keeps_local_catalog(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud()
            result=module.migrate(lib,'demo',remote,dry_run=True)
            self.assertTrue(result['dry_run']);self.assertEqual([],remote.calls)
            self.assertTrue((lib.app_dir('demo')/'catalog.sqlite3').exists())
            self.assertFalse((lib.app_dir('demo')/'cloud.json').exists())

    def test_migration_preserves_rows_foreign_keys_large_values_and_small_reference(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud()
            result=module.migrate(lib,'demo',remote)
            self.assertTrue(result['verified']);self.assertFalse((lib.app_dir('demo')/'catalog.sqlite3').exists())
            self.assertEqual('ready',remote.phase)
            reference=lib.app_dir('demo')/'cloud.json'
            self.assertLess(reference.stat().st_size,4096)
            self.assertGreater(len(remote.objects),0)
            entities=remote.tables['source_entities'];cols=remote.columns['source_entities']
            child=next(r for r in entities.values() if r[cols.index('entity_key')+1]=='child')
            self.assertEqual('parent',child[cols.index('parent_key')+1])

    def test_remote_corruption_keeps_database_and_does_not_activate_reference(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud(corrupt=True)
            with self.assertRaisesRegex(ValueError,'verification'):
                module.migrate(lib,'demo',remote)
            self.assertTrue((lib.app_dir('demo')/'catalog.sqlite3').exists())
            self.assertFalse((lib.app_dir('demo')/'cloud.json').exists())
            self.assertNotEqual('ready',remote.phase)

    def test_changed_catalog_without_matching_verified_backup_is_retained(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve())
            lib.record('demo','accounts',{'handle':'new','kind':'brand'})
            remote=MemoryCloud()
            with self.assertRaisesRegex(ValueError,'backup'):
                module.migrate(lib,'demo',remote)
            self.assertEqual([],remote.calls)
            self.assertTrue((lib.app_dir('demo')/'catalog.sqlite3').exists())

    def test_retry_after_activation_reverifies_before_local_removal(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud()
            with patch.object(module,'_unlink_checked',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):module.migrate(lib,'demo',remote)
            self.assertEqual('ready',remote.phase)
            remote.calls.clear()
            result=module.migrate(lib,'demo',remote)
            self.assertTrue(result['local_database_removed'])
            self.assertNotIn('import',remote.calls)
            self.assertIn('scan',remote.calls)

    def test_activation_retries_same_verification_identity_before_removing_source(self):
        from outpost.cloud import CloudRequestError
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud();original=remote.request;attempts=[]
            def request(action,data=None):
                if action=='finalize':
                    self.assertTrue((lib.app_dir('demo')/'catalog.sqlite3').exists())
                    attempts.append(data)
                    if len(attempts)<3:raise CloudRequestError(409)
                return original(action,data)
            remote.request=request
            with patch.object(module.time,'sleep'):
                result=module.migrate(lib,'demo',remote)
            self.assertTrue(result['local_database_removed'])
            self.assertEqual(3,len(attempts));self.assertEqual([attempts[0]]*3,attempts)

    def test_persistent_activation_conflict_keeps_source_and_connection_local(self):
        from outpost.cloud import CloudRequestError
        module=self.module()
        with tempfile.TemporaryDirectory() as temp:
            lib=self.fixture(Path(temp).resolve());remote=MemoryCloud();original=remote.request
            def request(action,data=None):
                if action=='finalize':raise CloudRequestError(409)
                return original(action,data)
            remote.request=request
            with patch.object(module.time,'sleep'),self.assertRaises(CloudRequestError):
                module.migrate(lib,'demo',remote)
            self.assertTrue((lib.app_dir('demo')/'catalog.sqlite3').exists())
            self.assertFalse((lib.app_dir('demo')/'cloud.json').exists())


class MemoryCloud:
    """Records the migration protocol; Worker tests execute the SQL itself."""
    def __init__(self,corrupt=False):
        self.reference={'format_version':1,'backend':'d1','app_id':'demo','worker_url':'https://example.test','database_id':'demo-db'}
        self.calls=[];self.tables={};self.columns={};self.objects={};self.phase='empty';self.corrupt=corrupt
    def upload_blob(self,data):
        sha=hashlib.sha256(data).hexdigest();self.objects[sha]=data;return sha
    def read_blob(self,sha):return self.objects[sha]
    def request(self,action,data=None):
        self.calls.append(action)
        if action=='init':
            if self.phase=='ready':return {'phase':self.phase}
            self.schema=data['schema'];self.phase='migrating'
            for s in self.schema:
                if s['type']=='table':self.columns[s['name']]=[c['name'] for c in s['columns']];self.tables[s['name']]={}
            return {'phase':self.phase}
        if action=='import':
            table=self.tables[data['table']]
            for row in data['rows']:
                if data['mode']=='insert':table[row[0]]=list(row)
                else:
                    for col,value in zip(data['columns'],row[1:]):table[row[0]][self.columns[data['table']].index(col)+1]=value
            return {'imported':len(data['rows'])}
        if action=='scan':
            rows=[r.copy() for ident,r in sorted(self.tables[data['table']].items()) if data['start']<=ident<=data['end']]
            if self.corrupt and data['table']=='accounts' and rows:rows[0][1]='corrupted'
            return {'rows':rows}
        if action=='finalize':self.phase='ready';return {'phase':self.phase,'revision':0}
        if action=='state':return {'phase':self.phase,'revision':0}
        raise AssertionError(action)


if __name__=='__main__':unittest.main()
