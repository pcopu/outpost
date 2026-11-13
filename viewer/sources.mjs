import {ensure,CHUNK,MANIFEST_LIMIT,bytes,canonical,digest,hash,validateRevision,validateFile} from './source-schema.mjs';
import {sourceZip,PACKAGE_LIMIT} from './source-package.mjs';

const bind=(db,sql,params=[])=>db.prepare(sql).bind(...params);
const schema=[
  'CREATE TABLE IF NOT EXISTS __outpost_sources(id TEXT PRIMARY KEY,video_id TEXT NOT NULL,label TEXT NOT NULL,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL)',
  'CREATE INDEX IF NOT EXISTS __outpost_sources_video ON __outpost_sources(video_id)',
  'CREATE TABLE IF NOT EXISTS __outpost_renders(id TEXT PRIMARY KEY,video_id TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES __outpost_sources(id),render_json TEXT NOT NULL,created_at TEXT NOT NULL)',
  'CREATE INDEX IF NOT EXISTS __outpost_renders_video ON __outpost_renders(video_id)',
  'CREATE TABLE IF NOT EXISTS __outpost_source_checks(id TEXT PRIMARY KEY,video_id TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES __outpost_sources(id),check_json TEXT NOT NULL,created_at TEXT NOT NULL)',
  'CREATE INDEX IF NOT EXISTS __outpost_source_checks_video ON __outpost_source_checks(video_id)'
];

async function database(env,app) {
  const bindings=JSON.parse(env.CATALOG_DATABASES||'{}');
  ensure(Object.hasOwn(bindings,app)&&env[bindings[app]],'Unknown cloud project',404);
  // A primary session keeps the viewer current when D1 read replicas are enabled.
  const binding=env[bindings[app]],db=binding.withSession?binding.withSession('first-primary'):binding;
  const meta=await db.prepare('SELECT * FROM __outpost_meta WHERE id=1').first();
  ensure(meta?.app_id===app && meta.phase==='ready','Cloud project is not ready',409);
  return {db,meta,enabled:Boolean(await db.prepare("SELECT name FROM sqlite_master WHERE name='__outpost_sources'").first())};
}
async function boundedJSON(object,limit=32*1024*1024) {
  ensure(object && object.size<=limit,'Archived metadata is missing or exceeds the limit',409);
  const data=new Uint8Array(await object.arrayBuffer());
  ensure(data.length<=limit,'Archived metadata exceeds limit',413);
  return {data,value:JSON.parse(new TextDecoder().decode(data))};
}
async function requestJSON(request) {
  ensure(request.method==='POST' && request.body,'POST body required',405);
  const reader=request.body.getReader();const blocks=[];let size=0;
  try {while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;ensure(size<=MANIFEST_LIMIT+16384,'Source request exceeds limit',413);blocks.push(value);}}
  finally {await reader.cancel();}
  const data=new Uint8Array(size);let at=0;for(const block of blocks){data.set(block,at);at+=block.length;}
  try{return JSON.parse(new TextDecoder().decode(data));}catch{ensure(false,'Invalid source JSON');}
}
async function snapshotLocation(env,app,file,cache=new Map()) {
  const loc=file.locator,base=`apps/${app}/snapshots/${loc.snapshot_id}`;
  async function read(name,limit){const key=base+'/'+name;if(!cache.has(key))cache.set(key,await boundedJSON(await env.MEDIA.get(key),limit));return cache.get(key);}
  const marker=(await read('COMMITTED.json',65536)).value;
  ensure(marker.app_id===app && marker.snapshot_id===loc.snapshot_id && marker.format_version===1,'Snapshot identity mismatch',409);
  const manifest=await read('manifest.json');
  ensure(await digest(manifest.data)===marker.manifest_sha256 && manifest.value.app_id===app && manifest.value.snapshot_id===loc.snapshot_id,'Snapshot checksum mismatch',409);
  const entry=manifest.value.entries.find(e=>e.path===loc.source_path);
  ensure(entry?.kind==='file' && entry.sha256===file.sha256 && entry.size===file.bytes,'Snapshot source mismatch',409);
  if(!marker.transport)return {binding:'MEDIA',key:base+'/'+loc.source_path,bytes:file.bytes};
  ensure(marker.transport==='packed-v1','Unsupported archive transport');
  const transport=await read('transport.json');
  ensure(await digest(transport.data)===marker.transport_sha256,'Snapshot transport checksum mismatch',409);
  const data=transport.value;
  ensure([1,2].includes(data.format_version) && Number.isSafeInteger(data.chunk_size) && data.chunk_size>0 && data.chunk_size<=128*1024*1024,'Unsupported archive chunk format');
  let position=0;const entries=manifest.value.entries.filter(e=>e.kind==='file');
  ensure(entries.length===data.files.length,'Archive file mapping mismatch',409);
  for(let i=0;i<entries.length;i++){const e=entries[i],s=data.files[i];ensure(s.path===e.path&&s.offset===position&&s.size===e.size,'Archive file range mismatch',409);position+=e.size;}
  ensure(position===data.total_bytes && data.chunks.length===Math.ceil(position/data.chunk_size),'Archive chunk mapping mismatch',409);
  const spec=data.files.find(f=>f.path===loc.source_path),parts=[];
  for(let i=0;i<data.chunks.length;i++) {
    const chunk=data.chunks[i];ensure(chunk.path===`payload/${String(i).padStart(6,'0')}` && hash(chunk.sha256),'Invalid archive chunk');
    const lo=Math.max(spec.offset,i*data.chunk_size),hi=Math.min(spec.offset+spec.size,(i+1)*data.chunk_size);
    if(lo<hi)parts.push({key:base+'/'+chunk.path,offset:lo-i*data.chunk_size,length:hi-lo,encoding:data.format_version===2?'gzip':undefined});
  }
  return {binding:'MEDIA',parts};
}
async function location(env,app,file) {
  if(file.locator.kind==='snapshot')return snapshotLocation(env,app,file);
  const parts=[];
  for(const chunk of file.locator.chunks) {
    const key=`sources/${app}/objects/${chunk.sha256}`;
    parts.push({key,offset:0,length:chunk.bytes});
  }
  return {binding:'MEDIA',parts};
}
async function registerObject(db,id,size) {
  // Receipts are content-addressed upload state, not creative catalog revisions.
  // A crash after R2 PUT can recover through the explicit confirm operation.
  await db.batch([
    db.prepare('CREATE TABLE IF NOT EXISTS __outpost_source_objects(sha256 TEXT PRIMARY KEY,bytes INTEGER NOT NULL CHECK(bytes>0))'),
    bind(db,'INSERT INTO __outpost_source_objects VALUES(?,?) ON CONFLICT(sha256) DO UPDATE SET bytes=CASE WHEN bytes=excluded.bytes THEN bytes ELSE -1 END',[id,size])]);
}
async function verifyFiles(env,app,db,files) {
  const chunks=new Map(),cache=new Map();
  for(const file of files) {
    if(file.locator.kind==='snapshot')await snapshotLocation(env,app,file,cache);
    else for(const chunk of file.locator.chunks){const previous=chunks.get(chunk.sha256);ensure(!previous||previous.bytes===chunk.bytes,'Conflicting source object sizes');chunks.set(chunk.sha256,chunk);}
  }
  if(chunks.size) {
    ensure(await db.prepare("SELECT name FROM sqlite_master WHERE name='__outpost_source_objects'").first(),'Source objects have not been verified',409);
    const row=await bind(db,"SELECT COUNT(*) AS n FROM __outpost_source_objects o JOIN json_each(?) e ON o.sha256=json_extract(e.value,'$.sha256') AND o.bytes=json_extract(e.value,'$.bytes')",[JSON.stringify([...chunks.values()])]).first();
    ensure(row.n===chunks.size,'Source object receipt is missing or mismatched',409);
  }
}
async function revisionRow(db,id) {
  const row=await bind(db,'SELECT * FROM __outpost_sources WHERE id=?',[id]).first();
  ensure(row,'Source revision not found',404);return row;
}
async function commit(db,meta,data,statements,identity) {
  ensure(typeof data.operation_id==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(data.operation_id)&&Number.isSafeInteger(data.expected_revision)&&data.expected_revision>=0,'Operation ID and expected catalog revision required');
  const operation='sources-'+data.operation_id;
  const operationHash=await digest(bytes(canonical({identity,expected_revision:data.expected_revision})));
  const prior=await bind(db,'SELECT hash,revision FROM __outpost_operations WHERE id=?',[operation]).first();
  if(prior){ensure(prior.hash===operationHash,'Operation identity conflict',409);return {revision:prior.revision,replayed:true,dry_run:Boolean(data.dry_run)};}
  ensure(meta.revision===data.expected_revision,'Catalog changed; read its current revision',409);
  if(data.dry_run)return {revision:meta.revision,dry_run:true};
  try {
    await db.batch([...schema.map(s=>db.prepare(s)),
      bind(db,'INSERT INTO __outpost_guard(id,valid) VALUES(1,CASE WHEN (SELECT revision FROM __outpost_meta WHERE id=1)=? THEN 1 ELSE 0 END) ON CONFLICT(id) DO UPDATE SET valid=excluded.valid',[data.expected_revision]),
      ...statements,db.prepare('UPDATE __outpost_meta SET revision=revision+1 WHERE id=1'),
      bind(db,'INSERT INTO __outpost_operations VALUES(?,?,?)',[operation,operationHash,data.expected_revision+1])]);
  }catch{ensure(false,'Source transaction conflicted; no changes committed',409);}
  return {revision:data.expected_revision+1,replayed:false,dry_run:false};
}

export async function sourceVideos(env,app) {
  const bindings=JSON.parse(env.CATALOG_DATABASES||'{}');if(!Object.hasOwn(bindings,app))return [];
  const {db,enabled}=await database(env,app);if(!enabled)return [];
  return (await db.prepare('SELECT s.video_id,MIN(s.label) AS title,COUNT(*) AS revisions,(SELECT COUNT(*) FROM __outpost_renders r WHERE r.video_id=s.video_id) AS render_count FROM __outpost_sources s GROUP BY s.video_id LIMIT 10000').all()).results;
}

export async function handleSources(request,env,{admin,authenticated,json,media}) {
  const url=new URL(request.url);
  const match=url.pathname.match(/^\/api\/sources\/([a-z][a-z0-9-]{0,62})\/(.+)$/);
  ensure(match,'Unknown source route',404);const [,app,route]=match;
  const isAdmin=await admin(request,env);
  ensure(isAdmin||await authenticated(request,env),'Unlock Outpost to continue',401);
  if(!isAdmin){
    const index=await env.MEDIA.get('viewer/index.json');
    ensure(index&&(await index.json()).projects.some(project=>project.id===app),'Project not found',404);
  }
  const {db,meta,enabled}=await database(env,app);
  if(route.startsWith('confirm/')) {
    ensure(isAdmin,'Source object administrator required',401);const id=route.slice(8);ensure(hash(id),'Invalid source object');
    const data=await requestJSON(request);ensure(Number.isSafeInteger(data.bytes)&&data.bytes>0&&data.bytes<=CHUNK,'Invalid source object size');
    const object=await env.MEDIA.head(`sources/${app}/objects/${id}`);ensure(object?.size===data.bytes,'Source object is missing or incomplete',409);
    if(!data.dry_run)await registerObject(db,id,data.bytes);
    return json({sha256:id,bytes:data.bytes,dry_run:Boolean(data.dry_run)});
  }
  if(route.startsWith('blob/')) {
    ensure(isAdmin,'Source object administrator required',401);
    const id=route.slice(5);ensure(hash(id),'Invalid source object');const key=`sources/${app}/objects/${id}`;
    if(request.method==='GET') {
      const object=await env.MEDIA.get(key);ensure(object,'Source object not found',404);
      return new Response(object.body,{headers:{'Content-Type':'application/octet-stream','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
    }
    ensure(request.method==='PUT','Unsupported source object method',405);
    const length=Number(request.headers.get('Content-Length'));
    ensure(request.headers.has('Content-Length')&&Number.isSafeInteger(length)&&length>0&&length<=CHUNK,'Bounded source chunk required',413);
    // Never buffer a request based only on an untrusted Content-Length header.
    const reader=request.body.getReader(),blocks=[];let size=0;
    try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;ensure(size<=length,'Source chunk exceeds declared size',413);blocks.push(value);}}finally{await reader.cancel();}
    const payload=new Uint8Array(size);let offset=0;for(const b of blocks){payload.set(b,offset);offset+=b.length;}
    ensure(size===length&&await digest(payload)===id,'Source object checksum mismatch');
    const existing=await env.MEDIA.head(key);
    if(existing){ensure(existing.size===length,'Source object conflict',409);await registerObject(db,id,length);return json({stored:true,existing:true,sha256:id});}
    const saved=await env.MEDIA.put(key,payload,{sha256:Uint8Array.from(id.match(/../g),v=>parseInt(v,16)),onlyIf:{etagDoesNotMatch:'*'},httpMetadata:{contentType:'application/octet-stream',cacheControl:'private, no-store'}});
    ensure(saved,'Source object changed concurrently; retry',409);await registerObject(db,id,length);return json({stored:true,sha256:id},201);
  }
  if(['publish','render','check'].includes(route)) {
    ensure(isAdmin,'Source publication administrator required',401);
    const data=await requestJSON(request),now=new Date().toISOString();let statements=[],identity,id;
    if(route==='publish') {
      const m=await validateRevision(data.revision,app);id=data.revision.revision_id;
      for(const parent of m.lineage){ensure(enabled,'Lineage revision is missing',409);const r=await revisionRow(db,parent.revision_id);ensure(r.video_id===parent.video_id,'Lineage identity mismatch',409);}
      if(!data.dry_run)await verifyFiles(env,app,db,m.files);
      if(enabled){const previous=await bind(db,'SELECT manifest_json FROM __outpost_sources WHERE id=?',[id]).first();if(previous)ensure(canonical(JSON.parse(previous.manifest_json))===canonical(m),'Immutable source conflict',409);}
      statements=[bind(db,'INSERT INTO __outpost_sources VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING',[id,m.video_id,data.revision.label,canonical(m),now])];
      identity={kind:'source',manifest:m};
    } else if(route==='render') {
      const r=data.render;
      ensure(r?.app_id===app&&typeof r.video_id==='string'&&hash(r.source_revision_id),'Invalid render identity');
      validateFile(r.output);ensure(r.output.role==='render'&&r.settings&&typeof r.settings==='object','Render output and settings required');
      ensure(['unverified','frozen_inputs'].includes(r.association?.status)&&typeof r.association.evidence==='string'&&r.association.evidence.length>0,'Render association evidence required');
      ensure(enabled,'Render source is missing',409);const source=await revisionRow(db,r.source_revision_id);
      ensure(source.video_id===r.video_id,'Render source belongs to another video',409);
      if(!data.dry_run)await verifyFiles(env,app,db,[r.output]);
      id=await digest(bytes(canonical(r)));identity={kind:'render',render:r};
      statements=[bind(db,'INSERT INTO __outpost_renders VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING',[id,r.video_id,r.source_revision_id,canonical(r),now])];
    } else {
      const c=data.check;
      ensure(c?.app_id===app&&typeof c.video_id==='string'&&hash(c.source_revision_id),'Invalid evidence identity');
      ensure(['restore','render','creative_approval'].includes(c.kind)&&['passed','failed','unverified','approved','rejected'].includes(c.status),'Invalid evidence kind/status');
      ensure(c.kind==='creative_approval'?['approved','rejected','unverified'].includes(c.status):['passed','failed','unverified'].includes(c.status),'Evidence status does not match kind');
      ensure(typeof c.observed_at==='string'&&/^\d{4}-\d\d-\d\dT.*(Z|[+-]\d\d:\d\d)$/.test(c.observed_at)&&Number.isFinite(Date.parse(c.observed_at))&&typeof c.evidence==='string'&&c.evidence.length>0&&c.evidence.length<=4000&&c.environment&&typeof c.environment==='object','Evidence needs time, environment and description');
      ensure(enabled,'Source evidence target is missing',409);const source=await revisionRow(db,c.source_revision_id);ensure(source.video_id===c.video_id,'Evidence belongs to another video',409);
      if(c.render_id){ensure(hash(c.render_id),'Invalid evidence render');const r=await bind(db,'SELECT source_id,video_id FROM __outpost_renders WHERE id=?',[c.render_id]).first();ensure(r?.source_id===c.source_revision_id&&r.video_id===c.video_id,'Evidence render belongs to another version',409);}
      if(c.kind==='render')ensure(c.render_id,'Render tests must identify the tested output');
      id=await digest(bytes(canonical(c)));identity={kind:'check',check:c};
      statements=[bind(db,'INSERT INTO __outpost_source_checks VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING',[id,c.video_id,c.source_revision_id,canonical(c),now])];
    }
    const result=await commit(db,meta,data,statements,identity);
    return json({...result,id,projection:'current_d1',publication_status:'archived_viewer_verification_pending'},result.replayed||data.dry_run?200:201);
  }
  if(route==='list') {
    const query=request.method==='POST'?await requestJSON(request):Object.fromEntries(url.searchParams);
    const video=query.video,offset=Number(query.offset||0);
    ensure(video&&Number.isSafeInteger(offset)&&offset>=0,'Video and valid offset required');
    if(!enabled)return json({revisions:[],renders:[],checks:[],catalog_revision:meta.revision});
    const result=await db.batch([
      bind(db,'SELECT id,label,created_at FROM __outpost_sources WHERE video_id=? ORDER BY created_at DESC,id LIMIT 100 OFFSET ?',[video,offset]),
      bind(db,'SELECT id,render_json FROM __outpost_renders WHERE video_id=? ORDER BY created_at DESC,id LIMIT 100 OFFSET ?',[video,offset]),
      bind(db,'SELECT id,check_json FROM __outpost_source_checks WHERE video_id=? ORDER BY created_at DESC,id LIMIT 100 OFFSET ?',[video,offset]),
      db.prepare('SELECT revision FROM __outpost_meta WHERE id=1')]);
    return json({revisions:result[0].results,renders:result[1].results.map(r=>({id:r.id,...JSON.parse(r.render_json)})),checks:result[2].results.map(r=>({id:r.id,...JSON.parse(r.check_json)})),catalog_revision:result[3].results[0].revision,next_offset:Math.max(result[0].results.length,result[1].results.length,result[2].results.length)===100?offset+100:null});
  }
  ensure(['GET','HEAD'].includes(request.method),'Unsupported source read method',405);
  const selected=route.match(/^(revision|render)\/([a-f0-9]{64})(?:\/files\/(\d+)|\/(package))?$/);
  ensure(selected&&enabled,'Source revision not found',404);
  const [,kind,id,index,packageRequest]=selected;let file,document;
  if(kind==='revision') {
    const row=await revisionRow(db,id);document={format_version:1,revision_id:id,label:row.label,manifest:JSON.parse(row.manifest_json)};
    if(index!==undefined)file=document.manifest.files[Number(index)];
  } else {
    const row=await bind(db,'SELECT render_json FROM __outpost_renders WHERE id=?',[id]).first();ensure(row,'Render not found',404);document=JSON.parse(row.render_json);if(index==='0')file=document.output;
  }
  if(packageRequest){
    ensure(kind==='revision','Select a source revision');
    ensure(document.manifest.files.length<=32&&document.manifest.files.reduce((n,f)=>n+f.bytes,0)+bytes(canonical(document)).length<=PACKAGE_LIMIT,'Project exceeds the 16 MiB / 32-file browser package limit; use the verified restore command',413);
    const files=[{name:'outpost-source.json',bytes:bytes(canonical(document))}];
    for(const f of document.manifest.files){
      const response=await media(new Request(url.origin+'/download'),env,{...f,name:f.path,mime:'application/octet-stream',location:await location(env,app,f)});
      let content;
      try{content=new Uint8Array(await response.arrayBuffer());}catch{ensure(false,'Source package bytes are missing or corrupt',409);}
      ensure(content.length===f.bytes&&await digest(content)===f.sha256,'Source package checksum mismatch',409);
      files.push({name:f.path,bytes:content});
    }
    return new Response(sourceZip(files),{headers:{'Content-Type':'application/zip','Content-Disposition':`attachment; filename="source-${id.slice(0,12)}.zip"`,'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
  }
  if(index===undefined)return json(document,200,url.searchParams.has('download')?{'Content-Disposition':`attachment; filename="source-${id.slice(0,12)}.json"`}:{});
  ensure(file,'Source file not found',404);
  const mime=/\.mp4$/i.test(file.path)?'video/mp4':'application/octet-stream';
  return media(request,env,{...file,name:file.path,mime,location:await location(env,app,file)});
}
