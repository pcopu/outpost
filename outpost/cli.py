"""JSON-output CLI. No hosted models, services, or background workers."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .archive import verify_snapshot
from .library import Library, READ_TABLES
from .storage import DirectoryStore, RcloneStore, WranglerStore, download, upload
from .util import write_json


def parser():
    p=argparse.ArgumentParser(prog='outpost',description='App-scoped marketing catalogs, original assets, and verified backups')
    p.add_argument('--version',action='version',version=__version__)
    p.add_argument('--root',type=Path,default=Path(os.environ.get('OUTPOST_ROOT',Path(__file__).resolve().parents[1]/'data')))
    groups=p.add_subparsers(dest='group',required=True)
    app=groups.add_parser('app').add_subparsers(dest='action',required=True)
    add=app.add_parser('add');add.add_argument('app');add.add_argument('--name',required=True);dry(add)
    app.add_parser('list')
    asset=groups.add_parser('asset').add_subparsers(dest='action',required=True)
    add=asset.add_parser('import');add.add_argument('app');add.add_argument('file',type=Path)
    add.add_argument('--kind',choices=['video','image','audio','document','other'],default='other')
    add.add_argument('--metadata',type=Path);dry(add)
    legacy=groups.add_parser('import-legacy');legacy.add_argument('app');legacy.add_argument('source',type=Path);dry(legacy)
    bundle=groups.add_parser('import-bundle');bundle.add_argument('app');bundle.add_argument('source',type=Path);bundle.add_argument('--seed-catalog',type=Path);bundle.add_argument('--reference-root',type=Path,action='append',default=[]);dry(bundle)
    lineage=groups.add_parser('lineage');lineage.add_argument('app');lineage.add_argument('entity_key');lineage.add_argument('--limit',type=int,default=100)
    metrics=groups.add_parser('metrics');metrics.add_argument('app');metrics.add_argument('--entity-key');metrics.add_argument('--metric');metrics.add_argument('--limit',type=int,default=100)
    cat=groups.add_parser('catalog').add_subparsers(dest='action',required=True)
    query=cat.add_parser('query');query.add_argument('app');query.add_argument('table',choices=READ_TABLES)
    query.add_argument('--filter',action='append',default=[]);query.add_argument('--limit',type=int,default=100)
    rec=cat.add_parser('record');rec.add_argument('app');rec.add_argument('table');rec.add_argument('--json',type=Path,required=True);dry(rec)
    export=cat.add_parser('export');export.add_argument('app');export.add_argument('--out',type=Path,required=True);dry(export)
    snap=groups.add_parser('snapshot').add_subparsers(dest='action',required=True)
    create=snap.add_parser('create');create.add_argument('app');create.add_argument('--hold-file',action='append',default=[],help='Explicit app-relative privacy hold: retain locally, declare absent from backup');dry(create)
    ls=snap.add_parser('list');ls.add_argument('app')
    check=snap.add_parser('verify');check.add_argument('directory',type=Path)
    exp=snap.add_parser('export');exp.add_argument('app');exp.add_argument('snapshot');exp.add_argument('--out',type=Path,required=True);dry(exp)
    up=snap.add_parser('upload');up.add_argument('app');up.add_argument('snapshot');transport(up);dry(up)
    down=snap.add_parser('download');down.add_argument('app');down.add_argument('snapshot');down.add_argument('--out',type=Path,required=True);down.add_argument('--reuse-from',type=Path);transport(down);dry(down)
    restore=groups.add_parser('restore');restore.add_argument('directory',type=Path);dry(restore)
    storage=groups.add_parser('storage').add_subparsers(dest='action',required=True)
    cfg=storage.add_parser('configure');transport(cfg,required=True);dry(cfg)
    storage.add_parser('show')
    return p


def dry(p):
    p.add_argument('--dry-run',action='store_true',help='Inspect and report without changing local or remote state')


def transport(p,required=False):
    store=p.add_mutually_exclusive_group(required=required)
    store.add_argument('--rclone',help='Existing remote:private-bucket reference; no credentials copied')
    store.add_argument('--wrangler-bucket',help='Private bucket accessed through an existing Wrangler login')
    store.add_argument('--local-store',type=Path,help='Offline/test object directory')
    p.add_argument('--account-id',help='Explicit Wrangler account ID (not a token)')
    p.add_argument('--private-confirmed',action='store_true',help='Operator has verified no public bucket URL/domain')


def reference(args):
    base={'format_version':1,'private_confirmed':args.private_confirmed}
    if args.local_store:return dict(base,backend='directory',path=str(args.local_store.expanduser().resolve()))
    if args.rclone:return dict(base,backend='rclone',remote=args.rclone)
    return dict(base,backend='wrangler',bucket=args.wrangler_bucket,account_id=args.account_id)


def store(args,lib):
    if not (args.local_store or args.rclone or args.wrangler_bucket):
        from .configuration import backend,load
        config=load(lib.root)
        if not config:raise ValueError('Configure storage or specify an existing private backend')
        return backend(config)
    if args.local_store:
        return DirectoryStore(args.local_store)
    if not args.private_confirmed:
        raise ValueError('Verify bucket privacy, then pass --private-confirmed; credentials stay in the existing CLI')
    return RcloneStore(args.rclone) if args.rclone else WranglerStore(args.wrangler_bucket,args.account_id)


def execute(args):
    lib=Library(args.root)
    dry_run=getattr(args,'dry_run',False)
    if args.group=='storage':
        from .configuration import load,save
        return save(lib.root,reference(args),dry_run) if args.action=='configure' else (load(lib.root) or {'configured':False})
    if args.group=='app':
        return lib.register(args.app,args.name,dry_run) if args.action=='add' else lib.apps()
    if args.group=='asset':
        metadata=json.loads(args.metadata.read_text()) if args.metadata else {}
        return lib.import_asset(args.app,args.file,args.kind,metadata,dry_run)
    if args.group=='import-legacy':
        return lib.import_legacy(args.app,args.source,dry_run)
    if args.group=='import-bundle':return lib.import_bundle(args.app,args.source,dry_run,args.seed_catalog,args.reference_root)
    if args.group=='metrics':
        filters={}
        if args.entity_key:filters['entity_key']=args.entity_key
        if args.metric:filters['metric']=args.metric
        return lib.rows(args.app,'source_metrics',filters,args.limit)
    if args.group=='lineage':
        from .util import connect
        with connect(lib.app_dir(args.app)/'catalog.sqlite3',readonly=True) as db:
            return [dict(row) for row in db.execute('SELECT * FROM source_relationships WHERE from_key=? OR to_key=? LIMIT ?',(args.entity_key,args.entity_key,args.limit))]
    if args.group=='catalog':
        if args.action=='query':
            filters={}
            for item in args.filter:
                if '=' not in item:
                    raise ValueError('Filters use column=value')
                key,value=item.split('=',1);filters[key]=value
            return lib.rows(args.app,args.table,filters,args.limit)
        if args.action=='record':
            return lib.record(args.app,args.table,json.loads(args.json.read_text()),dry_run)
        if args.out.exists() or args.out.is_symlink():
            raise FileExistsError('Catalog export destination already exists')
        result=lib.export_catalog(args.app)
        if not dry_run:
            write_json(args.out,result)
        return {'app_id':args.app,'destination':str(args.out),'dry_run':dry_run,'counts':{k:len(v) for k,v in result['tables'].items()}}
    if args.group=='restore':
        return lib.restore(args.directory,dry_run)
    if args.action=='create':
        return lib.snapshot(args.app,dry_run,args.hold_file)
    if args.action=='list':
        return [p.name for p in sorted((lib.app_dir(args.app)/'snapshots').glob('*')) if not p.name.startswith('.')]
    if args.action=='verify':
        manifest=verify_snapshot(args.directory)
        return {'app_id':manifest['app_id'],'snapshot_id':manifest['snapshot_id'],'verified':True,'counts':manifest['counts']}
    if args.action=='export':
        return lib.export_snapshot(args.app,args.snapshot,args.out,dry_run)
    if args.action=='upload':
        return upload(lib,args.app,args.snapshot,store(args,lib),dry_run)
    return download(store(args,lib),args.app,args.snapshot,args.out,dry_run,args.reuse_from)


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=execute(args)
    except (ValueError,OSError,RuntimeError,sqlite3.Error,KeyError,TypeError) as e:
        # Record values and subprocess output are never included in errors.
        print('outpost: '+str(e),file=sys.stderr)
        return 2
    print(json.dumps(result,indent=2,sort_keys=True,ensure_ascii=False))
    return 0
