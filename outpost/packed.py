"""Bounded-memory, bounded-disk snapshot packing for the existing Wrangler CLI."""
import json
import math
import os
from pathlib import Path
import shutil

from .archive import snapshot_id
from .util import digest, relative, write_json


def upload_payload(store,source,manifest,base,scratch,ensure_object):
    size=getattr(store,'chunk_size',128*1024*1024)
    if not 1<=size<=128*1024*1024:
        raise ValueError('Invalid transport chunk size')
    path=Path(scratch)/'chunk'
    chunks=[];files=[];position=0;used=0;output=None
    def flush():
        nonlocal used,output
        if output is None:
            return
        output.close();output=None
        name=f'payload/{len(chunks):06d}'
        chunks.append({'path':name,'sha256':digest(path),'size':used})
        ensure_object(store,base+'/'+name,path,scratch)
        used=0
    try:
        for entry in manifest['entries']:
            if entry['kind']!='file':
                continue
            files.append({'path':entry['path'],'offset':position,'size':entry['size']})
            with (source/entry['path']).open('rb') as src:
                remaining=entry['size']
                while remaining:
                    if output is None:
                        output=path.open('wb')
                    block=src.read(min(1024*1024,size-used,remaining))
                    if not block:
                        raise ValueError('Snapshot file shortened during packing')
                    output.write(block);used+=len(block);position+=len(block);remaining-=len(block)
                    if used==size:
                        flush()
        flush()
    finally:
        if output:
            output.close()
    descriptor={'format_version':1,'chunk_size':size,'files':files,'chunks':chunks,'total_bytes':position}
    out=Path(scratch)/'transport.json'
    write_json(out,descriptor)
    ensure_object(store,base+'/transport.json',out,scratch)
    return out,len(chunks)


def download_payload(store,base,manifest,marker,destination,scratch):
    descriptor_path=Path(scratch)/'transport.json'
    if not store.get(base+'/transport.json',descriptor_path) or digest(descriptor_path)!=marker['transport_sha256']:
        raise ValueError('Transport descriptor checksum mismatch')
    data=json.loads(descriptor_path.read_text())
    expected=[e for e in manifest['entries'] if e['kind']=='file']
    if data.get('format_version')!=1 or len(data['files'])!=len(expected):
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
        if chunk['path']!=f'payload/{i:06d}' or chunk['size']!=min(size,position-i*size):
            raise ValueError('Invalid transport chunk layout')
        snapshot_id(chunk['sha256'])
    cached=-1;chunk_path=Path(scratch)/'download-chunk'
    destination.mkdir(parents=True)
    for spec,entry in zip(data['files'],expected):
        path=destination/relative(spec['path'])
        path.parent.mkdir(parents=True,exist_ok=True)
        remaining=spec['size'];offset=spec['offset']
        with path.open('xb') as dst:
            while remaining:
                index=offset//size
                chunk=data['chunks'][index]
                if cached!=index:
                    if not store.get(base+'/'+chunk['path'],chunk_path) or chunk_path.stat().st_size!=chunk['size'] or digest(chunk_path)!=chunk['sha256']:
                        raise ValueError('Downloaded chunk checksum mismatch; incomplete directory retained')
                    cached=index
                amount=min(remaining,size-offset%size)
                with chunk_path.open('rb') as src:
                    src.seek(offset%size)
                    rest=amount
                    while rest:
                        block=src.read(min(rest,1024*1024))
                        if not block:
                            raise ValueError('Truncated transport chunk')
                        dst.write(block);rest-=len(block)
                remaining-=amount;offset+=amount
        if digest(path)!=entry['sha256']:
            raise ValueError('Restored file checksum mismatch')
        path.chmod(entry['mode'])
        os.utime(path,ns=(entry['mtime_ns'],entry['mtime_ns']))
