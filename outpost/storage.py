"""Write-once snapshot transports; download bytes and SHA-256 every object."""
import json
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import stat
import tempfile
import uuid
import selectors
import time

from .archive import snapshot_id, snapshot_path, validate_manifest, verify_snapshot
from .util import canonical, digest, relative, slug, write_json


def _wrangler_failure(operation,exit_code,text):
    """Keep actionable provider codes; never echo arbitrary response values."""
    codes=sorted(set(re.findall(r'(?:\[code:\s*|"code"\s*:\s*)([0-9]{3,6})(?![0-9])',text)))
    statuses=sorted(set(re.findall(r'(?i)(?:HTTP(?:/[0-9.]+)?\s*|"status"\s*:\s*)([45][0-9]{2})',text)))
    probe=text.lower()
    category='authentication/access' if '10000' in codes or any(s in statuses for s in ('401','403')) or 'authentication error' in probe else 'disk capacity' if any(s in probe for s in ('enospc','no space left')) else 'network' if any(s in probe for s in ('fetch failed','etimedout','econnreset','socket')) else 'CLI/account or transport'
    detail=f'exit {exit_code}, {category}'
    if codes:detail+=', provider '+','.join(codes)
    if statuses:detail+=', HTTP '+','.join(statuses)
    return f'Wrangler {operation} failed ({detail}); operation stopped, completion not confirmed'


class DirectoryStore:
    """Local test/offline object store. Never a live SQLite storage location."""
    def __init__(self,root):
        self.root=Path(root).resolve()

    @staticmethod
    def _directory(name,dir_fd=None):
        try:
            return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=dir_fd)
        except OSError as e:
            if e.errno in (errno.ELOOP,errno.ENOTDIR):
                raise ValueError('Unsafe symlink or non-directory in object store') from e
            raise

    @contextmanager
    def _parent(self,key,create=False):
        parts=relative(key).parts
        if self.root.is_symlink() or self.root.resolve()!=self.root:
            raise ValueError('Object store root changed through a symlink')
        fd=None
        try:
            if not self.root.exists():
                if not create:
                    yield None
                    return
                self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=self._directory(self.root)
            for part in parts[:-1]:
                try:
                    child=self._directory(part,fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(part,mode=0o700,dir_fd=fd)
                    except FileExistsError:
                        pass
                    child=self._directory(part,fd)
                os.close(fd);fd=child
            # Directory descriptors keep containment stable if path names are
            # swapped after validation; no parent symlink is ever followed.
            yield fd,parts[-1]
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _file(name,dir_fd):
        try:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno==errno.ELOOP:
                raise ValueError('Symlink object is not allowed') from e
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('Object must be a regular file')
        return fd

    @staticmethod
    def _hash(fd):
        h=hashlib.sha256()
        with os.fdopen(fd,'rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):
                h.update(block)
        return h.hexdigest()

    def get(self,key,destination):
        with self._parent(key) as parent:
            if parent is None:
                return False
            fd,name=parent
            source=self._file(name,fd)
            if source is None:
                return False
            with os.fdopen(source,'rb') as src,Path(destination).open('wb') as dst:
                shutil.copyfileobj(src,dst)
            return True

    def put(self,key,path):
        with self._parent(key,create=True) as (fd,name):
            temp='.outpost-put-'+uuid.uuid4().hex
            new=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                with os.fdopen(new,'wb') as dst,Path(path).open('rb') as src:
                    shutil.copyfileobj(src,dst)
                    dst.flush();os.fsync(dst.fileno())
                try:
                    # Publish a fully written object atomically, without
                    # replacing an existing object or following a symlink.
                    os.link(temp,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                except FileExistsError:
                    existing=self._file(name,fd)
                    if existing is None:
                        raise RuntimeError('Object changed concurrently; retry')
                    previous=self._hash(existing)
                    current=self._hash(self._file(temp,fd))
                    if previous!=current:
                        raise ValueError('Existing immutable object checksum mismatch')
                try:
                    os.fsync(fd)
                except OSError as e:
                    if e.errno not in (errno.EINVAL,errno.ENOTSUP):
                        raise
            finally:
                # Only this operation's private temporary copy is removed.
                os.unlink(temp,dir_fd=fd)


class RcloneStore:
    def __init__(self,remote):
        if not re.fullmatch(r'[A-Za-z0-9_-]+:[a-z0-9][a-z0-9.-]*(?:/[A-Za-z0-9_-]+)*',remote):
            raise ValueError('Use an existing rclone remote:private-bucket/optional-prefix reference')
        self.remote=remote.rstrip('/')

    def get(self,key,destination):
        relative(key)
        with Path(destination).open('wb') as f:
            result=subprocess.run(['rclone','cat',self.remote+'/'+key,'--s3-no-check-bucket','--retries','1','--contimeout','10s','--timeout','120s'],stdout=f,stderr=subprocess.PIPE)
        error=result.stderr.decode(errors='replace')
        if result.returncode and any(x in error for x in ('NoSuchKey','object not found','directory not found')):
            return False
        if result.returncode:
            raise RuntimeError('Rclone read failed; check the existing remote and bucket-specific access locally')
        return True

    def put(self,key,path):
        relative(key)
        result=subprocess.run(['rclone','copyto',str(path),self.remote+'/'+key,'--immutable','--s3-no-check-bucket','--retries','1','--contimeout','10s','--timeout','120s'],capture_output=True)
        if result.returncode:
            raise RuntimeError('Rclone immutable upload failed; no completion marker was written')


class WranglerStore:
    """Reference the installed Wrangler login/Keychain; never read its token."""
    packed=True
    chunk_size=64*1024*1024
    def __init__(self,bucket,account_id=None):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',bucket):
            raise ValueError('Invalid private R2 bucket name')
        if account_id is not None and not re.fullmatch('[0-9a-f]{32}',account_id):
            raise ValueError('Invalid account identifier')
        self.bucket=bucket;self.account_id=account_id

    def run(self,args,input_data=None):
        env=os.environ.copy()
        if self.account_id:
            env['CLOUDFLARE_ACCOUNT_ID']=self.account_id
        # Logs stay outside the app and its snapshots; credentials remain owned
        # by Wrangler. Disabling metrics also avoids an unrelated network call.
        env['WRANGLER_LOG_PATH']=str(Path(tempfile.gettempdir())/'outpost-wrangler.log')
        env['WRANGLER_SEND_METRICS']='false'
        try:
            return subprocess.run(['wrangler','r2','object']+args+['--remote'],env=env,capture_output=True,input=input_data,timeout=180)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError('Wrangler object operation timed out after 180 seconds; check the existing account/network and resume') from e

    def get(self,key,destination):
        relative(key)
        result=self.run(['get',self.bucket+'/'+key,'--file',str(destination)])
        output=(result.stdout+result.stderr).decode(errors='replace')
        if result.returncode and ('10007' in output or 'NoSuchKey' in output or 'The specified key does not exist' in output or 'object does not exist' in output):
            return False
        if result.returncode:
            raise RuntimeError(_wrangler_failure('object read',result.returncode,output))
        return True

    def put(self,key,path):
        relative(key)
        if Path(path).stat().st_size>300*1024*1024:
            raise ValueError('Wrangler upload supports files up to 300 MiB; use an authorized rclone remote for larger files')
        result=self.run(['put',self.bucket+'/'+key,'--file',str(path)])
        if result.returncode:
            raise RuntimeError(_wrangler_failure('object upload',result.returncode,(result.stdout+result.stderr).decode(errors='replace')))

    def put_payload(self,key,data):
        relative(key)
        if len(data)>129*1024*1024:raise ValueError('Payload exceeds bounded transport size')
        result=self.run(['put',self.bucket+'/'+key,'--pipe'],input_data=data)
        if result.returncode:raise RuntimeError(_wrangler_failure('piped upload',result.returncode,(result.stdout+result.stderr).decode(errors='replace')))

    def read_payload(self,key):
        """Bounded stdout transfer: no local payload/read-back file."""
        relative(key);env=os.environ.copy()
        if self.account_id:env['CLOUDFLARE_ACCOUNT_ID']=self.account_id
        env['WRANGLER_LOG_PATH']=str(Path(tempfile.gettempdir())/'outpost-wrangler.log')
        env['WRANGLER_SEND_METRICS']='false'
        data=bytearray();errors=bytearray();deadline=time.monotonic()+180
        command=['wrangler','r2','object','get',self.bucket+'/'+key,'--pipe','--remote']
        with subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout,selectors.EVENT_READ,'data');selector.register(proc.stderr,selectors.EVENT_READ,'error')
                    while selector.get_map():
                        left=deadline-time.monotonic()
                        if left<=0:raise RuntimeError('Wrangler streaming read timed out after 180 seconds')
                        for item,_ in selector.select(min(5,left)):
                            block=os.read(item.fileobj.fileno(),1024*1024)
                            if not block:selector.unregister(item.fileobj);continue
                            if item.data=='data':
                                data.extend(block)
                                if len(data)>129*1024*1024:raise ValueError('Remote chunk exceeds bounded transport size')
                            elif len(errors)<65536:errors.extend(block[:65536-len(errors)])
                code=proc.wait(timeout=max(1,deadline-time.monotonic()))
                if code:
                    error=(bytes(errors)+bytes(data[:4096])).decode(errors='replace')
                    if any(s in error for s in ('10007','NoSuchKey','The specified key does not exist','object does not exist')):raise FileNotFoundError('Remote object absent')
                    raise RuntimeError(_wrangler_failure('streaming read',code,error))
            except BaseException:
                if proc.poll() is None:proc.kill();proc.wait()
                raise
        return data

    def hash_object(self,key):
        try:return hashlib.sha256(self.read_payload(key)).hexdigest()
        except FileNotFoundError:return None


def prefix(app,ident):
    return f'apps/{slug(app)}/snapshots/{snapshot_id(ident)}'


def ensure_object(store,key,path,scratch):
    readback=Path(scratch)/'readback'
    expected=digest(path)
    if hasattr(store,'hash_object'):
        actual=store.hash_object(key)
        if actual is not None:
            if actual!=expected:raise ValueError('Existing immutable object checksum mismatch; refusing overwrite')
            return 'already_verified'
        store.put(key,path)
        if store.hash_object(key)!=expected:raise ValueError('Uploaded object read-back checksum mismatch')
        return 'uploaded_and_verified'
    if store.get(key,readback):
        if digest(readback)!=expected:
            raise ValueError('Existing immutable object checksum mismatch; refusing overwrite')
        return 'already_verified'
    store.put(key,path)
    if not store.get(key,readback) or digest(readback)!=expected:
        raise ValueError('Uploaded object read-back checksum mismatch')
    return 'uploaded_and_verified'


def ensure_payload(store,key,data):
    expected=hashlib.sha256(data).hexdigest();actual=store.hash_object(key)
    if actual is not None:
        if actual!=expected:raise ValueError('Existing immutable object checksum mismatch; refusing overwrite')
        return 'already_verified'
    store.put_payload(key,data)
    if store.hash_object(key)!=expected:raise ValueError('Uploaded object read-back checksum mismatch')
    return 'uploaded_and_verified'


def upload(lib,app,ident,store,dry_run=False):
    source=snapshot_path(lib,app,ident)
    manifest=verify_snapshot(source)
    if manifest['app_id']!=app:
        raise ValueError('Snapshot belongs to another app')
    base=prefix(app,ident)
    objects=[e['path'] for e in manifest['entries'] if e['kind']=='file']+['manifest.json']
    result={'app_id':app,'snapshot_id':ident,'objects':len(objects)+1,'prefix':base,'excluded':manifest.get('excluded',[]),'dry_run':dry_run}
    if getattr(store,'packed',False):
        size=getattr(store,'chunk_size',128*1024*1024)
        total=sum(e['size'] for e in manifest['entries'] if e['kind']=='file')
        result.update(transport='packed-v1',objects=(total+size-1)//size+3)
    if dry_run:
        return result
    # This lock protects a single Mac. Different devices produce independently
    # addressed snapshots; the manifest content hash is part of every prefix.
    with lib.writer(app),tempfile.TemporaryDirectory(prefix='outpost-readback-') as scratch:
        marker={'format_version':1,'app_id':app,'snapshot_id':ident,'manifest_sha256':digest(source/'manifest.json')}
        if getattr(store,'packed',False):
            from .packed import upload_payload
            descriptor,number=upload_payload(store,source,manifest,base,scratch,ensure_object)
            marker.update(transport='packed-v1',transport_sha256=digest(descriptor))
            result.update(transport='packed-v1',objects=number+3)
            ensure_object(store,base+'/manifest.json',source/'manifest.json',scratch)
        else:
            for rel in objects:
                ensure_object(store,base+'/'+rel,source/rel,scratch)
        commit=Path(scratch)/'commit.json'
        write_json(commit,marker)
        ensure_object(store,base+'/COMMITTED.json',commit,scratch)
        result['verified']=True
        receipts=lib.app_dir(app)/'receipts'
        receipts.mkdir(exist_ok=True)
        receipt=receipts/(ident+'.json')
        if not receipt.exists():
            write_json(receipt,result)
    return result


def download(store,app,ident,destination,dry_run=False,reuse_from=None):
    base=prefix(app,ident)
    destination=Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Download destination must not exist')
    if dry_run:
        return {'dry_run':True,'app_id':app,'snapshot_id':ident,'prefix':base}
    if reuse_from:
        reuse_from=Path(reuse_from).expanduser().resolve()
        if json.loads((reuse_from/'app.json').read_text())['app_id']!=app:raise ValueError('Restore cache belongs to another app')
    stats={}
    with tempfile.TemporaryDirectory(prefix='outpost-download-') as scratch:
        commit=Path(scratch)/'commit';mp=Path(scratch)/'manifest'
        if not store.get(base+'/COMMITTED.json',commit):
            raise ValueError('Snapshot upload has no verified completion marker')
        marker=json.loads(commit.read_text())
        if marker.get('app_id')!=app or marker.get('snapshot_id')!=ident:
            raise ValueError('Completion marker identity mismatch')
        if not store.get(base+'/manifest.json',mp) or digest(mp)!=marker['manifest_sha256']:
            raise ValueError('Manifest read-back checksum mismatch')
        manifest=validate_manifest(json.loads(mp.read_text()))
        if manifest['app_id']!=app or manifest['snapshot_id']!=ident:
            raise ValueError('Manifest identity mismatch')
        if marker.get('transport')=='packed-v1':
            from .packed import download_payload
            stats=download_payload(store,base,manifest,marker,destination,scratch,reuse_from)
        else:
            if reuse_from:raise ValueError('Cache-assisted verification requires a packed snapshot')
            destination.mkdir(parents=True)
            for entry in manifest['entries']:
                path=destination/relative(entry['path'])
                path.parent.mkdir(parents=True,exist_ok=True)
                if entry['kind']=='file':
                    if not store.get(base+'/'+entry['path'],path) or digest(path)!=entry['sha256']:
                        raise ValueError('Downloaded object checksum mismatch; incomplete directory retained')
                    path.chmod(entry['mode'])
                    os.utime(path,ns=(entry['mtime_ns'],entry['mtime_ns']))
        for entry in manifest['entries']:
            if entry['kind']=='symlink':
                path=destination/entry['path']
                path.parent.mkdir(parents=True,exist_ok=True)
                if not (path.parent/entry['target']).resolve().is_relative_to(destination.resolve()):
                    raise ValueError('Downloaded symlink escapes snapshot')
                path.symlink_to(entry['target'])
                os.utime(path,ns=(entry['mtime_ns'],entry['mtime_ns']),follow_symlinks=False)
        shutil.copyfile(mp,destination/'manifest.json')
        verify_snapshot(destination)
    return dict({'verified':True,'app_id':app,'snapshot_id':ident,'destination':str(destination)},**stats)


def verify_remote(store,app,ident,expected_manifest=None):
    """Hash all remote packed bytes and file ranges without a restore tree."""
    base=prefix(app,ident)
    with tempfile.TemporaryDirectory(prefix='outpost-stream-check-') as scratch:
        commit=Path(scratch)/'commit';mp=Path(scratch)/'manifest'
        if not store.get(base+'/COMMITTED.json',commit):raise ValueError('Snapshot upload has no verified completion marker')
        marker=json.loads(commit.read_text())
        if marker.get('app_id')!=app or marker.get('snapshot_id')!=ident:raise ValueError('Completion marker identity mismatch')
        if not store.get(base+'/manifest.json',mp) or digest(mp)!=marker['manifest_sha256']:raise ValueError('Manifest read-back checksum mismatch')
        manifest=validate_manifest(json.loads(mp.read_text()))
        if manifest['app_id']!=app or manifest['snapshot_id']!=ident:raise ValueError('Manifest identity mismatch')
        if expected_manifest and digest(expected_manifest)!=digest(mp):raise ValueError('Remote manifest differs from expected local snapshot')
        if marker.get('transport')!='packed-v1':raise ValueError('Streaming verification requires a packed snapshot')
        from .packed import download_payload
        stats=download_payload(store,base,manifest,marker,None,scratch,verify_only=True)
        return dict(verified=True,app_id=app,snapshot_id=ident,manifest_sha256=digest(mp),catalog_sha256=next(e['sha256'] for e in manifest['entries'] if e['path']=='catalog.sqlite3'),full_local_restore=False,held_files=len(manifest.get('held',[])),**stats)
