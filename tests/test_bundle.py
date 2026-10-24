import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sqlite3



class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        from outpost.library import Library
        self.lib=Library(self.base/'library');self.lib.register('alpha','Alpha');self.lib.register('beta','Beta')

    def bundle(self):
        from outpost.util import canonical,digest
        root=self.base/'bundle';root.mkdir()
        payload={'views':42,'unknown':{'keep':['all',None]},'token_usage':{'input':20},'authorization':'Business authorization description'}
        record={'record_id':'r1','kind':'organic_post','platform':'example','entity_id':'post-1','entity_key':'post:1','source_path':'original.json','source_sha256':'1'*64,'source_coordinate':'/posts/0','capture_context':{'original_window':None},'payload':payload,'raw_payload_sha256':hashlib.sha256(canonical(payload)).hexdigest(),'outer_unknown':{'preserve':True}}
        streams={'records':[record],'entities':[{'entity_key':'post:1','kind':'organic_post','source_id':'post-1','platform':'example'}],
                 'metrics':[{'key':'m1','record_id':'r1','entity_key':'post:1','captured_at':None,'metric':'views','value':42,'unit':'count','raw':{'source_value':42}}, {'key':'m2','record_id':'r1','entity_key':'post:1','captured_at':None,'metric':'missing','value':None,'unit':None}],
                 'relationships':[{'from_key':'post:1','to_key':'post:1','relationship':'source_reference','record_id':'r1','raw':{'reviewed':True}}],
                 'asset_links':[{'record_id':'r1','entity_key':'post:1','sha256':hashlib.sha256(b'image').hexdigest(),'role':'image','ordinal':0}]}
        manifest={'version':1,'app_id':'alpha','bundle_id':'bundle-one','assets':[],'raw_sources':[],'provenance':{'scope':'synthetic marketing'},'excluded':[],'sanitized':[]}
        for name,rows in streams.items():
            path=root/(name+'.jsonl');path.write_bytes(b''.join(canonical(row)+b'\n' for row in rows));manifest[name]=path.name
        image=root/'image.jpg';image.write_bytes(b'image')
        manifest['assets']=[{'path':image.name,'sha256':digest(image),'original_path':'original/image.jpg','media_kind':'image'}]
        (root/'bundle.json').write_text(json.dumps(manifest))
        return root

    def test_bundle_is_queryable_isolated_lossless_and_idempotent(self):
        root=self.bundle()
        result=self.lib.import_bundle('alpha',root)
        self.assertEqual(1,result['records'])
        rows=self.lib.rows('alpha','source_records')
        self.assertEqual(42,json.loads(rows[0]['payload_json'])['views'])
        self.assertEqual({'preserve':True},json.loads(rows[0]['header_json'])['outer_unknown'])
        self.assertEqual([],self.lib.rows('beta','source_records'))
        metrics=self.lib.rows('alpha','source_metrics')
        self.assertEqual({'42','null'},{x['value_json'] for x in metrics})
        self.assertEqual(1,len(self.lib.rows('alpha','source_relationships')))
        self.assertEqual(1,len(self.lib.rows('alpha','source_asset_links')))
        self.assertTrue(self.lib.import_bundle('alpha',root)['already_imported'])
        self.assertEqual(1,len(self.lib.rows('alpha','source_records')))
        snap=self.lib.snapshot('alpha');out=self.base/'export';self.lib.export_snapshot('alpha',snap['snapshot_id'],out)
        from outpost.library import Library
        restored=Library(self.base/'restore');restored.restore(out)
        self.assertEqual(rows,restored.rows('alpha','source_records'))

    def test_dry_run_and_conflicts_never_partially_change_records(self):
        root=self.bundle()
        def inventory():return {str(p.relative_to(self.lib.root)):(p.lstat().st_size,p.lstat().st_mtime_ns) for p in self.lib.root.rglob('*')}
        before=inventory()
        self.lib.import_bundle('alpha',root,dry_run=True)
        self.assertEqual(before,inventory())
        self.lib.import_bundle('alpha',root)
        path=root/'records.jsonl';row=json.loads(path.read_text());row['payload']['views']=99;path.write_text(json.dumps(row)+'\n')
        with self.assertRaises(ValueError):self.lib.import_bundle('alpha',root)
        self.assertEqual(42,json.loads(self.lib.rows('alpha','source_records')[0]['payload_json'])['views'])

    def test_actual_credentials_and_cross_app_input_are_rejected(self):
        root=self.bundle()
        with self.assertRaises(ValueError):self.lib.import_bundle('beta',root)
        path=root/'records.jsonl';row=json.loads(path.read_text());row['payload']['cookie']='session='+'x'*30;path.write_text(json.dumps(row)+'\n')
        with self.assertRaises(ValueError):self.lib.import_bundle('alpha',root)
        self.assertEqual([],self.lib.rows('alpha','source_records'))

    def test_explicit_snapshot_holds_keep_files_local_and_restore_visible_gaps(self):
        from outpost.util import write_json
        from outpost.library import Library
        root=self.bundle();self.lib.import_bundle('alpha',root)
        path=self.lib.app_dir('alpha')/'files/private-response.json'
        write_json(path,{'tt_chain_token':'opaque-auth-value','posts':[{'id':'post-1'}]})
        with self.assertRaisesRegex(ValueError,'Unsafe files'):
            self.lib.snapshot('alpha')
        snap=self.lib.snapshot('alpha',held_files=['files/private-response.json'])
        source=Path(snap['path']);manifest=json.loads((source/'manifest.json').read_text())
        self.assertEqual('files/private-response.json',manifest['held'][0]['path'])
        self.assertTrue(path.exists());self.assertFalse((source/'files/private-response.json').exists())
        restored=Library(self.base/'held-restore');restored.restore(source)
        self.assertTrue((restored.app_dir('alpha')/'snapshot-holds.json').exists())
        again=restored.snapshot('alpha');self.assertEqual(snap['snapshot_id'],again['snapshot_id'])
        with self.assertRaises(ValueError):self.lib.snapshot('alpha',held_files=['../outside'])

    def test_external_references_require_explicit_roots_and_matching_hash(self):
        root=self.bundle();external=self.base/'approved-marketing';external.mkdir()
        target=external/'image.jpg';target.write_bytes(b'image')
        (root/'image.jpg').unlink();(root/'image.jpg').symlink_to(target)
        with self.assertRaises(ValueError):self.lib.import_bundle('alpha',root)
        result=self.lib.import_bundle('alpha',root,reference_roots=[external])
        self.assertEqual(1,result['records'])
        self.assertEqual(b'image',target.read_bytes())

    def test_seed_catalog_is_reused_without_duplicating_payload(self):
        root=self.bundle();record=json.loads((root/'records.jsonl').read_text())
        record['record_id']='wrapper-r1';record['outer_fields']={'record_key':'r1'}
        (root/'records.jsonl').write_text(json.dumps(record)+'\n')
        for name in ('metrics','relationships','asset_links'):
            p=root/(name+'.jsonl');rows=[json.loads(line) for line in p.read_text().splitlines()]
            for row in rows:
                if row.get('record_id')=='r1':row['record_id']='wrapper-r1'
            p.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        seed=self.base/'export.sqlite3';db=sqlite3.connect(seed)
        db.execute('CREATE TABLE records(record_key TEXT PRIMARY KEY,payload_json TEXT NOT NULL)')
        db.execute('INSERT INTO records VALUES (?,?)',('r1',json.dumps(record['payload'])));db.commit();db.close()
        self.lib.import_bundle('alpha',root,seed_catalog=seed)
        physical=self.lib.rows('alpha','source_events')[0]
        self.assertIsNone(physical['payload_json'])
        self.assertEqual('r1',physical['payload_record_key'])
        self.assertEqual(record['payload'],json.loads(self.lib.rows('alpha','source_records')[0]['payload_json']))

    def test_redacted_records_preserve_original_and_verified_stored_hashes(self):
        from outpost.util import canonical
        root=self.bundle();p=root/'records.jsonl';record=json.loads(p.read_text())
        original={'token':{'token':'eyJ'+'a'*30+'.'+'b'*30+'.'+'c'*30,'expires':123},'unknown':['keep']}
        record['raw_payload_sha256']=hashlib.sha256(canonical(original)).hexdigest()
        record['payload']={'token':{'token':'[REDACTED]','expires':123},'unknown':['keep']}
        record['stored_payload_sha256']=hashlib.sha256(canonical(record['payload'])).hexdigest()
        record['redacted_fields']=1;record['redaction_pointers']=['/token/token']
        p.write_text(json.dumps(record)+'\n');self.lib.import_bundle('alpha',root)
        row=self.lib.rows('alpha','source_records')[0]
        self.assertEqual(record['raw_payload_sha256'],row['raw_payload_sha256'])
        self.assertEqual(record['stored_payload_sha256'],json.loads(row['header_json'])['stored_payload_sha256'])
        self.assertEqual(123,json.loads(row['payload_json'])['token']['expires'])
