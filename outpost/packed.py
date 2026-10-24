"""Bounded-memory, bounded-disk snapshot packing for the existing Wrangler CLI."""
import json
import gzip
import io
from contextlib import nullcontext
import hashlib
import math
import os
from pathlib import Path
import shutil

from .archive import snapshot_id
from .util import copy_file,digest, relative, write_json


def upload_payload(store,source,manifest,base,scratch,ensure_object):
    size=getattr(store,'chunk_size',128*1024*1024)
    previous=Path(scratch)/'previous-transport.json'
    version=2 if getattr(store,'compress_payload',True) else 1
    if store.get(base+'/transport.json',previous):
        prior=json.loads(previous.read_text())
        version=prior.get('format_version')
        size=prior.get('chunk_size')
    if version not in (1,2):raise ValueError('Unsupported transport version')
    if not 1<=size<=128*1024*1024:
        raise ValueError('Invalid transport chunk size')
    path=Path(scratch)/('compressed-chunk' if version==2 else 'chunk')
    chunks=[];files=[];position=0;used=0;output=None;raw_output=None;raw_hash=hashlib.sha256()
    def flush():
        nonlocal used,output,raw_output,raw_hash
        if output is None:
            return
        output.close();output=None
        if raw_output:raw_output.close();raw_output=None
        name=f'payload/{len(chunks):06d}'
        stored=path
        chunk={'path':name,'sha256':digest(path),'size':used}
        if version==2:
            chunk.update(raw_sha256=raw_hash.hexdigest(),raw_size=used,encoding='gzip',sha256=digest(stored),size=stored.stat().st_size)
        chunks.append(chunk)
        ensure_object(store,base+'/'+name,stored,scratch)
        used=0;raw_hash=hashlib.sha256()
    try:
        for entry in manifest['entries']:
            if entry['kind']!='file':
                continue
            files.append({'path':entry['path'],'offset':position,'size':entry['size']})
            with (source/entry['path']).open('rb') as src:
                remaining=entry['size']
                while remaining:
                    if output is None:
                        raw_output=path.open('wb')
                        output=gzip.GzipFile(filename='',fileobj=raw_output,mode='wb',mtime=0,compresslevel=1) if version==2 else raw_output
                    block=src.read(min(1024*1024,size-used,remaining))
                    if not block:
                        raise ValueError('Snapshot file shortened during packing')
                    output.write(block);raw_hash.update(block);used+=len(block);position+=len(block);remaining-=len(block)
                    if used==size:
                        flush()
        flush()
    finally:
        if output:
            output.close()
        if raw_output:raw_output.close()
    descriptor={'format_version':version,'chunk_size':size,'files':files,'chunks':chunks,'total_bytes':position}
    out=Path(scratch)/'transport.json'
    write_json(out,descriptor)
    ensure_object(store,base+'/transport.json',out,scratch)
    return out,len(chunks)


def download_payload(store,base,manifest,marker,destination,scratch,reuse_from=None,verify_only=False):
    descriptor_path=Path(scratch)/'transport.json'
    if not store.get(base+'/transport.json',descriptor_path) or digest(descriptor_path)!=marker['transport_sha256']:
        raise ValueError('Transport descriptor checksum mismatch')
    data=json.loads(descriptor_path.read_text())
    expected=[e for e in manifest['entries'] if e['kind']=='file']
    version=data.get('format_version')
    if version not in (1,2) or len(data['files'])!=len(expected):
        raise ValueError('Invalid transport descriptor')
    size=data['chunk_size'];position=0
    if not isinstance(size,int) or not 1<=size<=128*1024*1024:
        raise ValueError('Invalid transport chunk size')
    for spec,entry in zip(data['files'],expected):
        if spec!={'path':entry['path'],'offset':position,'size':entry['size']}:
            raise ValueError('Transport files do not match manifest')
        position+=entry['size']
    if position!=data['total_bytes'] or len(data['chunks'])!=math.ceil(position/size):
        raise ValueError('Invalid transport byte counts')
    for i,chunk in enumerate(data['chunks']):
        raw_size=min(size,position-i*size)
        if chunk['path']!=f'payload/{i:06d}':
            raise ValueError('Invalid transport chunk layout')
        if version==1 and chunk['size']!=raw_size:raise ValueError('Invalid transport chunk size')
        if version==2:
            if chunk.get('encoding')!='gzip' or chunk.get('raw_size')!=raw_size or not 0<chunk['size']<=raw_size+1024*1024:raise ValueError('Invalid compressed chunk layout')
            snapshot_id(chunk['raw_sha256'])
        snapshot_id(chunk['sha256'])
    cached=-1;chunk_path=Path(scratch)/'download-chunk';decoded=Path(scratch)/'decoded-chunk';local_clones=0;memory=None
    if not verify_only:destination.mkdir(parents=True)
    for spec,entry in zip(data['files'],expected):
        path=None if verify_only else destination/relative(spec['path'])
        if path:path.parent.mkdir(parents=True,exist_ok=True)
        remaining=spec['size'];offset=spec['offset']
        cache=Path(reuse_from)/entry['path'] if reuse_from and not verify_only else None
        use_cache=bool(cache and not cache.is_symlink() and cache.is_file() and cache.resolve().is_relative_to(Path(reuse_from).resolve()) and digest(cache)==entry['sha256'])
        if use_cache:copy_file(cache,path);local_clones+=1
        dst=None if use_cache or verify_only else path.open('xb');remote_hash=hashlib.sha256()
        try:
            while remaining:
                index=offset//size
                chunk=data['chunks'][index]
                if cached!=index:
                    if verify_only and hasattr(store,'read_payload'):
                        payload=store.read_payload(base+'/'+chunk['path'])
                        if len(payload)!=chunk['size'] or hashlib.sha256(payload).hexdigest()!=chunk['sha256']:raise ValueError('Downloaded chunk checksum mismatch')
                        if version==2:
                            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as src:raw=src.read(chunk['raw_size']+1)
                            if len(raw)!=chunk['raw_size'] or hashlib.sha256(raw).hexdigest()!=chunk['raw_sha256']:raise ValueError('Decoded chunk checksum mismatch')
                        else:raw=payload
                        memory=io.BytesIO(raw)
                    elif not store.get(base+'/'+chunk['path'],chunk_path) or chunk_path.stat().st_size!=chunk['size'] or digest(chunk_path)!=chunk['sha256']:
                        raise ValueError('Downloaded chunk checksum mismatch; incomplete directory retained')
                    elif version==2:
                        with gzip.open(chunk_path,'rb') as src,decoded.open('wb') as unpacked:
                            remaining_raw=chunk['raw_size']
                            while remaining_raw:
                                block=src.read(min(remaining_raw,1024*1024))
                                if not block:raise ValueError('Truncated compressed chunk')
                                unpacked.write(block);remaining_raw-=len(block)
                            if src.read(1):raise ValueError('Compressed chunk exceeds declared size')
                        if digest(decoded)!=chunk['raw_sha256']:raise ValueError('Decoded chunk checksum mismatch')
                    cached=index
                amount=min(remaining,size-offset%size)
                with (nullcontext(memory) if memory is not None else (decoded if version==2 else chunk_path).open('rb')) as src:
                    src.seek(offset%size)
                    rest=amount
                    while rest:
                        block=src.read(min(rest,1024*1024))
                        if not block:
                            raise ValueError('Truncated transport chunk')
                        remote_hash.update(block)
                        if dst:dst.write(block)
                        rest-=len(block)
                remaining-=amount;offset+=amount
        finally:
            if dst:dst.close()
        if remote_hash.hexdigest()!=entry['sha256'] or (path and digest(path)!=entry['sha256']):
            raise ValueError('Restored file checksum mismatch')
        if path:
            path.chmod(entry['mode'])
            os.utime(path,ns=(entry['mtime_ns'],entry['mtime_ns']))
    return {'remote_bytes_hashed':position,'local_content_clones':local_clones,'files_verified':len(expected)}
