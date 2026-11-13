"""JSON-output CLI for explicit cloud/local operations; no background workers."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .archive import verify_snapshot
from .library import Library, READ_TABLES
from .storage import DirectoryStore, RcloneStore, WranglerStore, download, upload,verify_remote
from .gateway import WorkerStore
from .util import write_json


def parser():
    p=argparse.ArgumentParser(prog='outpost',description='Private app-scoped cloud catalogs, original assets, and verified recovery')
    p.add_argument('--version',action='version',version=__version__)
    p.add_argument('--root',type=Path,default=Path(os.environ.get('OUTPOST_ROOT',Path(__file__).resolve().parents[1]/'data')))
    groups=p.add_subparsers(dest='group',required=True)
    sources=groups.add_parser('source',help='Exact video sources and render provenance').add_subparsers(dest='action',required=True)
    freeze=sources.add_parser('freeze');freeze.add_argument('app');freeze.add_argument('directory',type=Path)
    freeze.add_argument('--spec',type=Path,required=True);freeze.add_argument('--out',type=Path,required=True);dry(freeze)
    for action in ('publish','render'):
        command=sources.add_parser(action);command.add_argument('app');command.add_argument('directory',type=Path)
        command.add_argument('--manifest',type=Path,required=True);command.add_argument('--revision',type=int,required=True)
        command.add_argument('--operation-id',required=True);dry(command)
        if action=='render':command.add_argument('--spec',type=Path,required=True)
    show=sources.add_parser('show');show.add_argument('app');show.add_argument('revision_id')
    listing=sources.add_parser('list');listing.add_argument('app');listing.add_argument('video');listing.add_argument('--offset',type=int,default=0)
    check=sources.add_parser('check',help='Append restore/render/approval evidence without changing source history')
    check.add_argument('app');check.add_argument('--json',type=Path,required=True);check.add_argument('--revision',type=int,required=True);check.add_argument('--operation-id',required=True);dry(check)
    restore_source=sources.add_parser('restore');restore_source.add_argument('app');restore_source.add_argument('revision_id')
    restore_source.add_argument('--out',type=Path,required=True);restore_source.add_argument('--resume',action='store_true')
    restore_source.add_argument('--missing-only',action='store_true');dry(restore_source)
    cloud=groups.add_parser('cloud',help='Query and update Cloudflare catalogs without a local database').add_subparsers(dest='action',required=True)
    for action in ('status','schema'):
        command=cloud.add_parser(action);command.add_argument('app')
    migrate=cloud.add_parser('migrate',help='Verify every cloud row, then replace local SQLite with a connection reference')
    migrate.add_argument('app');migrate.add_argument('--reference',type=Path,required=True);dry(migrate)
    sql=cloud.add_parser('sql',help='Read-only SELECT; large scalar values are expanded after SQL evaluation')
    sql.add_argument('app');sql.add_argument('--sql',required=True);sql.add_argument('--params',default='[]')
    transaction=cloud.add_parser('transact',help='Atomic, revision-checked writes with a reusable operation identity')
    transaction.add_argument('app');transaction.add_argument('--json',type=Path,required=True)
    transaction.add_argument('--revision',type=int,required=True);transaction.add_argument('--operation-id',required=True);dry(transaction)
    offload=groups.add_parser('offload',help='Upload missing archives, verify every remote file, then free local storage')
    offload.add_argument('app')
    offload.add_argument('--source',type=Path,action='append',default=[],help='Explicit source file or directory to import and offload; excluded files remain local')
    offload.add_argument('--copies',type=Path,action='append',default=[],help='Explicit staging file/directory; remove only exact matches to verified archives, retain unmatched files')
    transport(offload);dry(offload)
    app=groups.add_parser('app').add_subparsers(dest='action',required=True)
    add=app.add_parser('add');add.add_argument('app');add.add_argument('--name',required=True);dry(add)
    app.add_parser('list')
    production=groups.add_parser('production',help='Versioned sources, transcripts and reusable generation files').add_subparsers(dest='action',required=True)
    init=production.add_parser('init');init.add_argument('app');init.add_argument('directory',type=Path);dry(init)
    ingest=production.add_parser('import');ingest.add_argument('app');ingest.add_argument('directory',type=Path);dry(ingest)
    show=production.add_parser('show');show.add_argument('app');show.add_argument('video',nargs='?')
    asset=groups.add_parser('asset').add_subparsers(dest='action',required=True)
    add=asset.add_parser('import');add.add_argument('app');add.add_argument('file',type=Path)
    add.add_argument('--kind',choices=['video','image','audio','document','other'],default='other')
    add.add_argument('--metadata',type=Path);dry(add)
    fetch=asset.add_parser('fetch',help='Retrieve one verified file into an explicit external destination')
    fetch.add_argument('app');fetch.add_argument('source_path',help='App-relative files/... path from the snapshot/catalog')
    fetch.add_argument('--snapshot',required=True,help='Full authoritative snapshot SHA-256')
    fetch.add_argument('--out',type=Path,required=True,help='Fresh external output file; parent must exist, no automatic cache')
    transport(fetch);dry(fetch)
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
    remote=snap.add_parser('remote-verify');remote.add_argument('app');remote.add_argument('snapshot');transport(remote)
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
    store.add_argument('--worker-url',help='HTTPS origin of the private Outpost archive Worker')
    p.add_argument('--token-env',default='OUTPOST_ARCHIVE_TOKEN',help='Existing environment variable containing the archive admin token')
    p.add_argument('--token-file',type=Path,help='Existing owner-only credential file; only its path is saved')
    p.add_argument('--token-key',default='ADMIN_TOKEN',help='Credential key within the existing file')
    store.add_argument('--local-store',type=Path,help='Offline/test object directory')
    p.add_argument('--account-id',help='Explicit Wrangler account ID (not a token)')
    p.add_argument('--private-confirmed',action='store_true',help='Operator has verified no public bucket URL/domain')


def reference(args):
    base={'format_version':1,'private_confirmed':args.private_confirmed}
    if args.local_store:return dict(base,backend='directory',path=str(args.local_store.expanduser().resolve()))
    if args.rclone:return dict(base,backend='rclone',remote=args.rclone)
    if args.worker_url:
        config=dict(base,backend='worker',url=args.worker_url,token_env=args.token_env)
        if args.token_file:config.update(token_file=str(args.token_file.expanduser().absolute()),token_key=args.token_key)
        return config
    return dict(base,backend='wrangler',bucket=args.wrangler_bucket,account_id=args.account_id)


def store(args,lib):
    if not (args.local_store or args.rclone or args.wrangler_bucket or args.worker_url):
        from .configuration import backend,load
        config=load(lib.root)
        if not config:raise ValueError('Configure storage or specify an existing private backend')
        return backend(config)
    if args.local_store:
        return DirectoryStore(args.local_store)
    if not args.private_confirmed:
        raise ValueError('Verify bucket privacy, then pass --private-confirmed; credentials stay in the existing CLI')
    if args.worker_url:return WorkerStore(args.worker_url,args.token_env,args.token_file,args.token_key)
    return RcloneStore(args.rclone) if args.rclone else WranglerStore(args.wrangler_bucket,args.account_id)


def execute(args):
    lib=Library(args.root)
    dry_run=getattr(args,'dry_run',False)
    if args.group=='source':
        from .source_versions import freeze,SourceClient,publish,archived_reader,record_render
        from .source_restore import restore,validate,real_path,canonical
        if args.action=='freeze':
            spec=json.loads(args.spec.read_text())
            if spec.get('app_id')!=args.app:raise ValueError('Source description belongs to another app')
            result=freeze(args.directory,spec)
            target=real_path(args.out)
            if target.exists():raise FileExistsError('Frozen manifest already exists; choose a new path')
            if not dry_run:
                with target.open('xb') as output:output.write(canonical(result)+b'\n')
            return {'revision_id':result['revision_id'],'files':len(result['manifest']['files']),'dry_run':dry_run,'destination':str(target)}
        from .cloud import for_app
        remote=for_app(lib,args.app)
        if not remote:raise ValueError('Exact source publication requires this app’s cloud catalog reference')
        client=SourceClient(remote)
        if args.action=='check':
            from .util import secret_data
            evidence=json.loads(args.json.read_text())
            if evidence.get('app_id')!=args.app or secret_data(evidence):raise ValueError('Invalid source evidence or possible credential')
            return client.request('check',{'check':evidence,'expected_revision':args.revision,'operation_id':args.operation_id,'dry_run':dry_run})
        if args.action=='list':return client.request('list',{'video':args.video,'offset':args.offset})
        if args.action in ('show','restore'):
            revision=client.request('revision/'+args.revision_id);validate(revision)
            if revision['manifest']['app_id']!=args.app:raise ValueError('Source revision belongs to another app')
            if args.action=='show':return revision
            return restore(revision,args.out,lambda file:client.fetch(revision,file),resume=args.resume,missing_only=args.missing_only,dry_run=dry_run)
        revision=json.loads(args.manifest.read_text())
        if args.action=='render':return record_render(client,revision,args.directory,json.loads(args.spec.read_text()),args.revision,args.operation_id,dry_run)
        return publish(client,revision,args.directory,args.revision,args.operation_id,dry_run,archive_fetch=archived_reader(lib,remote))
    if args.group=='cloud':
        from .cloud import CloudCatalog,for_app
        if args.action=='migrate':
            from .cloud_migration import migrate
            remote=CloudCatalog(json.loads(args.reference.read_text()))
            return migrate(lib,args.app,remote,dry_run,progress=lambda message:print(message,file=sys.stderr,flush=True))
        remote=for_app(lib,args.app)
        if args.action=='status':
            return {'app_id':args.app,'mode':'cloud' if remote else 'local',
                    'local_database_exists':(lib.app_dir(args.app)/'catalog.sqlite3').exists(),
                    'cloud':remote.request('state') if remote else None}
        if not remote:raise ValueError('This app has no cloud catalog reference; migrate it first')
        if args.action=='schema':return remote.request('schema')
        if args.action=='sql':return remote.sql(args.sql,json.loads(args.params))
        return remote.mutate(json.loads(args.json.read_text()),args.revision,args.operation_id,dry_run)
    if args.group=='offload':
        from .offload import offload
        backend=store(args,lib)
        if isinstance(backend,DirectoryStore):backend.packed=True
        return offload(lib,args.app,backend,args.source,dry_run,
                       progress=lambda message:print(message,file=sys.stderr,flush=True),copies=args.copies)
    if args.group=='storage':
        from .configuration import load,save
        return save(lib.root,reference(args),dry_run) if args.action=='configure' else (load(lib.root) or {'configured':False})
    if args.group=='app':
        return lib.register(args.app,args.name,dry_run) if args.action=='add' else lib.apps()
    if args.group=='production':
        from .production import initialize_workspace, import_production, show_production
        if args.action=='init':return initialize_workspace(lib,args.app,args.directory,dry_run)
        if args.action=='import':return import_production(lib,args.app,args.directory,dry_run)
        return show_production(lib,args.app,args.video)
    if args.group=='asset':
        if args.action=='fetch':
            from .fetch import fetch_file
            return fetch_file(lib,store(args,lib),args.app,args.snapshot,args.source_path,args.out,dry_run)
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
        from .cloud import for_app
        remote=for_app(lib,args.app)
        if remote:
            return remote.sql('SELECT * FROM source_relationships WHERE from_key=? OR to_key=? LIMIT ?',
                              [args.entity_key,args.entity_key,args.limit])['rows']
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
    if args.action=='remote-verify':
        expected=lib.root/'apps'/args.app/'snapshots'/args.snapshot/'manifest.json'
        return verify_remote(store(args,lib),args.app,args.snapshot,expected if expected.exists() else None)
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
