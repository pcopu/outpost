// A small bounded delivery ZIP, never the source of truth. All entries have
// already been validated and SHA-256 checked by the caller. No compression,
// symlinks, executable modes, or ZIP64 ambiguities.
export const PACKAGE_LIMIT=16*1024*1024;
function crc32(bytes){let crc=0xffffffff;for(const b of bytes){crc^=b;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}
function record(size,values){const data=new Uint8Array(size),view=new DataView(data.buffer);for(const [at,value,width] of values)width===2?view.setUint16(at,value,true):view.setUint32(at,value,true);return data;}
function concat(blocks){const data=new Uint8Array(blocks.reduce((n,b)=>n+b.length,0));let offset=0;for(const block of blocks){data.set(block,offset);offset+=block.length;}return data;}
export function sourceZip(files){
  const local=[],central=[];let offset=0;
  for(const file of files){const name=new TextEncoder().encode(file.name),crc=crc32(file.bytes),size=file.bytes.length;
    const header=record(30,[[0,0x04034b50,4],[4,20,2],[6,0x800,2],[14,crc,4],[18,size,4],[22,size,4],[26,name.length,2]]);
    local.push(header,name,file.bytes);
    central.push(record(46,[[0,0x02014b50,4],[4,20,2],[6,20,2],[8,0x800,2],[16,crc,4],[20,size,4],[24,size,4],[28,name.length,2],[42,offset,4]]),name);
    offset+=header.length+name.length+size;
  }
  const directory=concat(central),end=record(22,[[0,0x06054b50,4],[8,files.length,2],[10,files.length,2],[12,directory.length,4],[16,offset,4]]);
  return concat([...local,directory,end]);
}
