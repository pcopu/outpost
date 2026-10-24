import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def test_asset_fetch_uses_configured_storage_without_rehydrating_library(self):
        from outpost.library import Library
        from outpost.storage import DirectoryStore, upload
        from outpost.configuration import save
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp).resolve();root=base/'data';objects=base/'objects'
            lib=Library(root);lib.register('demo','Demo')
            original=base/'media.jpg';original.write_bytes(b'only this requested asset')
            lib.import_asset('demo',original,'image')
            row=lib.rows('demo','vault_assets')[0]
            snapshot=lib.snapshot('demo');upload(lib,'demo',snapshot['snapshot_id'],DirectoryStore(objects))
            save(root,{'format_version':1,'backend':'directory','path':str(objects),'private_confirmed':True})
            original.unlink();(lib.app_dir('demo')/row['stored_path']).unlink()
            out=base/'retrieved.jpg'
            command=[sys.executable,'-m','outpost','--root',str(root),'asset','fetch','demo',row['stored_path'],
                     '--snapshot',snapshot['snapshot_id'],'--out',str(out)]
            preview=subprocess.run(command+['--dry-run'],capture_output=True,text=True)
            self.assertEqual(0,preview.returncode,preview.stderr)
            self.assertTrue(json.loads(preview.stdout)['metadata_verified'])
            self.assertFalse(out.exists())
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertTrue(json.loads(result.stdout)['verified'])
            self.assertEqual(b'only this requested asset',out.read_bytes())
            self.assertFalse((lib.app_dir('demo')/row['stored_path']).exists())

    def test_source_launcher_works_outside_checkout(self):
        launcher=Path(__file__).resolve().parents[1]/'bin/outpost'
        with tempfile.TemporaryDirectory() as temp:
            result=subprocess.run([sys.executable,str(launcher),'--root',str(Path(temp)/'data'),'app','list'],cwd=temp,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual([],json.loads(result.stdout))

    def test_storage_reference_is_idempotent_and_used_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'data';objects=Path(temp)/'objects'
            def run(*args):
                result=subprocess.run([sys.executable,'-m','outpost','--root',str(root),*args],capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stderr)
                return json.loads(result.stdout)
            run('storage','configure','--local-store',str(objects),'--dry-run')
            self.assertFalse(root.exists())
            cfg=run('storage','configure','--local-store',str(objects))
            self.assertEqual(cfg,run('storage','configure','--local-store',str(objects)))
            self.assertEqual(cfg,run('storage','show'))
            run('app','add','demo','--name','Demo')
            snap=run('snapshot','create','demo')['snapshot_id']
            self.assertTrue(run('snapshot','upload','demo',snap)['verified'])
            self.assertTrue(run('snapshot','download','demo',snap,'--out',str(Path(temp)/'download'))['verified'])

    def test_cli_registration_asset_query_and_json_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'data'
            def run(*args,ok=True):
                result=subprocess.run([sys.executable,'-m','outpost','--root',str(root),*args],capture_output=True,text=True)
                self.assertEqual(0 if ok else 2,result.returncode,result.stderr)
                return json.loads(result.stdout) if ok else result.stderr
            run('app','add','demo','--name','Demo','--dry-run')
            self.assertFalse(root.exists())
            run('app','add','demo','--name','Demo')
            file=Path(temp)/'creative.mp4';file.write_bytes(b'example')
            run('asset','import','demo',str(file))
            query=run('catalog','query','demo','vault_assets','--filter','media_kind=other')
            self.assertEqual(1,len(query))
            export=Path(temp)/'catalog.json'
            run('catalog','export','demo','--out',str(export))
            self.assertEqual(1,len(json.loads(export.read_text())['tables']['vault_assets']))
            run('catalog','export','demo','--out',str(export),ok=False)
            run('app','add','../bad','--name','Bad',ok=False)

    def test_credential_values_are_never_echoed_on_invalid_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'data'
            subprocess.run([sys.executable,'-m','outpost','--root',str(root),'app','add','demo','--name','Demo'],check=True,capture_output=True)
            record=Path(temp)/'record.json'
            token='sk-'+'x'*40
            record.write_text(json.dumps({'handle':'creator','kind':'brand','display_name':'api_key="'+token+'"'}))
            result=subprocess.run([sys.executable,'-m','outpost','--root',str(root),'catalog','record','demo','accounts','--json',str(record)],capture_output=True,text=True)
            self.assertEqual(2,result.returncode)
            self.assertNotIn(token,result.stdout+result.stderr)
