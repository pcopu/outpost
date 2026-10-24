"""Verified individual-file copies from immutable snapshots, with no rehydration."""
from contextlib import closing, contextmanager
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import uuid
import zlib

from .archive import snapshot_id, validate_manifest
from .storage import prefix
from .util import relative


BLOCK=1024*1024
METADATA_LIMIT=32*1024*1024


@contextmanager
def _output_parent(destination,library_root):
    path=Path(destination).expanduser().absolute()
    if '..' in path.parts or path.is_relative_to(library_root):
        raise ValueError('Use an external destination without traversal; fetch never writes into the managed library')
    fd=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            try:child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP,errno.ENOTDIR):raise ValueError('Destination parents must be real directories, not symlinks') from e
                if e.errno==errno.ENOENT:raise ValueError('Destination parent must already exist; create an external output directory first') from e
                raise
            os.close(fd);fd=child
        try:os.stat(path.name,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise FileExistsError('Fetch destination already exists; use a fresh file path')
        yield fd,path.name,path
    finally:os.close(fd)


def _metadata(store,key,stats,limit=METADATA_LIMIT):
    data=bytearray()
    with closing(store.iter_object(key)) as blocks:
        for block in blocks:
            if len(data)+len(block)>limit:raise ValueError('Remote metadata exceeds the bounded size limit')
            data.extend(block);stats['remote_metadata_bytes']+=len(block)
    return data


def _transport(data,manifest):
    version=data.get('format_version');size=data.get('chunk_size')
    if version not in (1,2) or type(size) is not int or not 1<=size<=128*BLOCK:
        raise ValueError('Transport format or chunk size is invalid')
    expected=[e for e in manifest['entries'] if e['kind']=='file'];position=0
    if len(data['files'])!=len(expected):raise ValueError('Transport files do not match manifest')
    for spec,entry in zip(data['files'],expected):
        if type(entry['size']) is not int or spec!={'path':entry['path'],'offset':position,'size':entry['size']}:
            raise ValueError('Transport file ranges do not match manifest')
        position+=entry['size']
    if data['total_bytes']!=position or len(data['chunks'])!=math.ceil(position/size):
        raise ValueError('Transport total byte counts are invalid')
    for i,chunk in enumerate(data['chunks']):
        raw_size=min(size,position-i*size)
        if chunk['path']!=f'payload/{i:06d}' or type(chunk['size']) is not int:
            raise ValueError('Transport chunk layout is invalid')
        snapshot_id(chunk['sha256'])
        if version==1 and chunk['size']!=raw_size:raise ValueError('Transport chunk size is invalid')
        if version==2:
            snapshot_id(chunk['raw_sha256'])
            if chunk.get('encoding')!='gzip' or chunk.get('raw_size')!=raw_size or not 0<chunk['size']<=raw_size+BLOCK:
                raise ValueError('Transport compressed chunk layout is invalid')
    return version,size


def _chunk_blocks(store,key,chunk,version):
    encoded=hashlib.sha256();raw=hashlib.sha256();encoded_size=0;raw_size=0
    decoder=zlib.decompressobj(31) if version==2 else None
    limit=chunk['raw_size'] if decoder else chunk['size']
    with closing(store.iter_object(key)) as blocks:
        for block in blocks:
            encoded_size+=len(block);encoded.update(block)
            if encoded_size>chunk['size']:raise ValueError('Downloaded chunk size/checksum mismatch')
            pending=block
            while True:
                try:part=decoder.decompress(pending,BLOCK) if decoder else pending
                except zlib.error as e:raise ValueError('Compressed chunk checksum/decode failure') from e
                raw_size+=len(part);raw.update(part)
                if raw_size>limit:raise ValueError('Decoded chunk exceeds declared size')
                if part:yield part
                if not decoder:break
                if decoder.unused_data:raise ValueError('Compressed chunk has trailing data')
                pending=decoder.unconsumed_tail
                if not pending and len(part)<BLOCK:break
    expected_raw=chunk['raw_sha256'] if decoder else chunk['sha256']
    if (encoded_size!=chunk['size'] or encoded.hexdigest()!=chunk['sha256'] or
        raw_size!=limit or raw.hexdigest()!=expected_raw or (decoder and not decoder.eof)):
        raise ValueError('Downloaded/decoded chunk checksum mismatch')


def fetch_file(lib,store,app,ident,source_path,destination,dry_run=False):
    """Copy one regular marketing file. Never mutate a catalog, cache or remote."""
    lib.app_dir(app);base=prefix(app,ident);source_path=str(relative(source_path))
    if not source_path.startswith('files/'):
        raise ValueError('Select an app-relative marketing file under files/, not a catalog or identity file')
    stats={'remote_metadata_bytes':0,'remote_payload_bytes':0,'materialized_bytes':0,'chunks_fetched':0}
    with _output_parent(destination,lib.root) as (parent,name,path):
        marker=json.loads(_metadata(store,base+'/COMMITTED.json',stats,65536))
        if marker.get('format_version')!=1 or marker.get('app_id')!=app or marker.get('snapshot_id')!=ident:
            raise ValueError('Completion marker identity mismatch')
        mp=_metadata(store,base+'/manifest.json',stats)
        if hashlib.sha256(mp).hexdigest()!=marker['manifest_sha256']:raise ValueError('Manifest checksum mismatch')
        manifest=validate_manifest(json.loads(mp))
        if manifest['app_id']!=app or manifest['snapshot_id']!=ident:raise ValueError('Manifest identity mismatch')
        entry=next((e for e in manifest['entries'] if e['path']==source_path),None)
        if not entry or entry['kind']!='file':raise ValueError('Select a regular backed-up file; symlinks, held and absent paths cannot be fetched')
        if type(entry['size']) is not int:raise ValueError('Manifest file size is invalid')
        packed=marker.get('transport');selected=[];data=None
        if packed=='packed-v1':
            td=_metadata(store,base+'/transport.json',stats)
            if hashlib.sha256(td).hexdigest()!=marker['transport_sha256']:raise ValueError('Transport checksum mismatch')
            data=json.loads(td);version,size=_transport(data,manifest)
            spec=next(f for f in data['files'] if f['path']==source_path)
            start=spec['offset'];end=start+entry['size']
            if entry['size']:selected=list(range(start//size,(end-1)//size+1))
        elif packed is not None:raise ValueError('Unsupported snapshot transport')
        result={'app_id':app,'snapshot_id':ident,'source_path':source_path,'sha256':entry['sha256'],
                'destination':str(path),'cache_behavior':'external_copy_only','dry_run':dry_run,
                'metadata_verified':True,'verified':False,'verification_scope':'selected_file_only','planned_file_bytes':entry['size'],
                'planned_chunks':len(selected),'planned_payload_bytes':sum(data['chunks'][i]['size'] for i in selected) if data else entry['size']}
        if not dry_run:
            temp='.outpost-fetch-'+uuid.uuid4().hex
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                with os.fdopen(fd,'wb') as output:
                    file_hash=hashlib.sha256()
                    if data is not None:
                        for index in selected:
                            chunk=data['chunks'][index];offset=index*size
                            with closing(_chunk_blocks(store,base+'/'+chunk['path'],chunk,version)) as blocks:
                                for block in blocks:
                                    lo=max(0,start-offset);hi=min(len(block),end-offset)
                                    if lo<hi:
                                        part=block[lo:hi];output.write(part);file_hash.update(part);stats['materialized_bytes']+=len(part)
                                    offset+=len(block)
                            stats['remote_payload_bytes']+=chunk['size'];stats['chunks_fetched']+=1
                    else:
                        with closing(store.iter_object(base+'/'+source_path)) as blocks:
                            for block in blocks:
                                stats['remote_payload_bytes']+=len(block)
                                if stats['remote_payload_bytes']>entry['size']:raise ValueError('Fetched file size/checksum mismatch')
                                output.write(block);file_hash.update(block);stats['materialized_bytes']+=len(block)
                    if stats['materialized_bytes']!=entry['size'] or file_hash.hexdigest()!=entry['sha256']:
                        raise ValueError('Fetched file checksum mismatch')
                    output.flush();os.fsync(output.fileno())
                    os.utime(output.fileno(),ns=(entry['mtime_ns'],entry['mtime_ns']))
                # A same-directory link publishes complete verified bytes with
                # no replacement, even if another process creates the target.
                os.link(temp,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
                try:os.fsync(parent)
                except OSError as e:
                    if e.errno not in (errno.EINVAL,errno.ENOTSUP):raise
                result['verified']=True
            finally:os.unlink(temp,dir_fd=parent)
        stats['remote_bytes_fetched']=stats['remote_metadata_bytes']+stats['remote_payload_bytes']
        return dict(result,**stats)
