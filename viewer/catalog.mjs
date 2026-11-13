// App-isolated D1 catalogs. Only the existing archive administrator can use
// these routes; viewer sessions never grant database or import access.
const encoder = new TextEncoder();
const hex = bytes => [...new Uint8Array(bytes)].map(n => n.toString(16).padStart(2,'0')).join('');
const hash = async value => hex(await crypto.subtle.digest('SHA-256', encoder.encode(JSON.stringify(value))));
const internal = '__outpost_';
export class CatalogError extends Error {
  constructor(status, message) {super(message);this.status=status;}
}
function requireValue(condition, message='Invalid catalog request', status=400) {
  if (!condition) throw new CatalogError(status,message);
}
function identifier(value) {
  requireValue(typeof value==='string' && /^[A-Za-z][A-Za-z0-9_]*$/.test(value) && !value.startsWith(internal));
  return '"'+value+'"';
}
async function body(request, maximum=16*1024*1024) {
  requireValue(request.method==='POST' && request.body,'POST JSON body required');
  const reader=request.body.getReader();const blocks=[];let size=0;
  try {
    while (true) {
      const {done,value}=await reader.read();if(done)break;
      size+=value.length;requireValue(size<=maximum,'Catalog request exceeds its bounded size',413);blocks.push(value);
    }
  } finally {await reader.cancel();}
  const bytes=new Uint8Array(size);let at=0;for(const block of blocks){bytes.set(block,at);at+=block.length;}
  try {return JSON.parse(new TextDecoder().decode(bytes));}
  catch {throw new CatalogError(400,'Invalid JSON body');}
}
function parameter(value) {
  if (value && typeof value==='object' && Object.keys(value).length===1 && typeof value.$outpost_blob==='string') {
    requireValue(/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.$outpost_blob));
    return Uint8Array.from(atob(value.$outpost_blob),c=>c.charCodeAt(0)).buffer;
  }
  requireValue(value===null || typeof value==='string' || typeof value==='number' && Number.isFinite(value));
  return value;
}
function wire(value) {
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    const bytes=value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer,value.byteOffset,value.byteLength);
    let raw='';for(let i=0;i<bytes.length;i+=8192)raw+=String.fromCharCode(...bytes.subarray(i,i+8192));
    return {$outpost_blob:btoa(raw)};
  }
  return value;
}
const bind=(db,sql,params=[])=>db.prepare(sql).bind(...params.map(parameter));
async function all(db,sql,params=[]) {return (await bind(db,sql,params).all()).results;}
async function state(db) {
  const exists=await db.prepare("SELECT name FROM sqlite_master WHERE name='__outpost_meta'").first();
  if(!exists)return null;
  return db.prepare('SELECT * FROM __outpost_meta WHERE id=1').first();
}
const guard=(db,condition,params=[])=>bind(db,
  `INSERT INTO __outpost_guard(id,valid) VALUES(1,CASE WHEN ${condition} THEN 1 ELSE 0 END) ON CONFLICT(id) DO UPDATE SET valid=excluded.valid`,params);
function table(meta,name,views=false) {
  identifier(name);
  const entry=JSON.parse(meta.manifest_json).schema.find(s=>s.name===name && (s.type==='table'||views&&s.type==='view'));
  requireValue(entry,'Unknown catalog table');return entry;
}
function columns(entry,names) {
  const allowed=new Set(entry.columns.map(c=>c.name));
  requireValue(Array.isArray(names) && names.length>0 && names.length<=99 && new Set(names).size===names.length && names.every(c=>allowed.has(c)),'Unknown catalog column');
  return names.map(identifier).join(',');
}
async function transaction(db,statements) {
  try {return await db.batch(statements);}
  catch {throw new CatalogError(409,'Catalog transaction failed or conflicted; no changes committed');}
}
function operation(data) {
  requireValue(typeof data.operation_id==='string' && /^[a-zA-Z0-9_-]{1,100}$/.test(data.operation_id),'Invalid operation identity');
  requireValue(Number.isSafeInteger(data.expected_revision) && data.expected_revision>=0,'Expected catalog revision required');
  requireValue(Array.isArray(data.statements) && data.statements.length>0 && data.statements.length<=5000,'Invalid transaction statement count');
  for(const s of data.statements) {
    requireValue(typeof s.sql==='string' && encoder.encode(s.sql).length<=100000 && /^\s*(INSERT|UPDATE|DELETE)\b/i.test(s.sql) && !s.sql.toLowerCase().includes(internal) && !s.sql.includes(';'),'Only bounded data mutations are accepted');
    requireValue(Array.isArray(s.params) && s.params.length<=100,'Invalid statement parameters');s.params.forEach(parameter);
  }
}

export async function handleCatalog(request,env,{admin,json}) {
  requireValue(await admin(request,env),'Authentication required',401);
  const match=new URL(request.url).pathname.match(/^\/api\/catalog\/([a-z][a-z0-9-]{0,62})\/([a-z-]+)(?:\/([a-f0-9]{64}))?$/);
  requireValue(match,'Catalog route not found',404);
  const [,app,action,objectHash]=match;
  const bindings=typeof env.CATALOG_DATABASES==='string' ? JSON.parse(env.CATALOG_DATABASES) : {};
  requireValue(Object.hasOwn(bindings,app) && env[bindings[app]],'Unknown cloud catalog',404);
  const db=env[bindings[app]];
  if(action==='blob') {
    requireValue(objectHash,'Cloud value hash required');
    const key=`catalogs/${app}/blobs/${objectHash}`;
    if(request.method==='GET') {
      const object=await env.MEDIA.get(key);requireValue(object,'Cloud value not found',404);
      return new Response(object.body,{headers:{'Content-Type':'application/octet-stream','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
    }
    requireValue(request.method==='PUT','Unsupported cloud value method',405);
    const length=Number(request.headers.get('Content-Length'));
    requireValue(Number.isSafeInteger(length) && length>=0 && length<=64*1024*1024,'Cloud value requires a bounded content length',413);
    const old=await env.MEDIA.head(key);
    if(old){requireValue(old.size===length,'Cloud value conflicts',409);return json({stored:true,existing:true,sha256:objectHash});}
    const checksum=Uint8Array.from(objectHash.match(/../g),x=>parseInt(x,16));
    const saved=await env.MEDIA.put(key,request.body||new Uint8Array(),{sha256:checksum,onlyIf:{etagDoesNotMatch:'*'},httpMetadata:{contentType:'application/octet-stream',cacheControl:'private, no-store'}});
    requireValue(saved,'Cloud value changed concurrently',409);return json({stored:true,sha256:objectHash},201);
  }
  let meta=await state(db);
  if(action==='init') {
    const data=await body(request);requireValue(data.app_id===app && /^[a-f0-9]{64}$/.test(data.source_sha256),'Migration identity mismatch');
    requireValue(data.backup?.catalog_sha256===data.source_sha256 && /^[a-f0-9]{64}$/.test(data.backup?.snapshot_id),'Verified source backup required');
    requireValue(Array.isArray(data.schema) && data.schema.length>0 && data.schema.length<=250,'Invalid catalog schema');
    for(const [name,value] of Object.entries(data.sequences||{})) {
      identifier(name);requireValue(Number.isSafeInteger(value)&&value>=0&&data.schema.some(s=>s.type==='table'&&s.name===name&&/AUTOINCREMENT/i.test(s.sql)),'Invalid autoincrement counter');
    }
    const schemaHash=await hash(data.schema);
    if(meta){requireValue(meta.app_id===app && meta.source_sha256===data.source_sha256 && meta.schema_sha256===schemaHash,'Migration conflicts with existing catalog',409);return json({phase:meta.phase,revision:meta.revision});}
    const occupied=await all(db,"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name NOT LIKE 'd1_%'");
    requireValue(occupied.length===0,'Target database is not empty',409);
    for(const s of data.schema) {
      identifier(s.name);requireValue(['table','index','view'].includes(s.type) && typeof s.sql==='string' && new RegExp('^CREATE\\s+(?:UNIQUE\\s+)?'+s.type+'\\b','i').test(s.sql.trim()),'Invalid schema statement');
      requireValue(!s.sql.toLowerCase().includes(internal) && encoder.encode(s.sql).length<=100000,'Invalid schema statement');
    }
    const stmts=[db.prepare('CREATE TABLE __outpost_meta(id INTEGER PRIMARY KEY CHECK(id=1),app_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,schema_sha256 TEXT NOT NULL,phase TEXT NOT NULL,revision INTEGER NOT NULL,manifest_json TEXT NOT NULL,verification_sha256 TEXT)'),
      db.prepare('CREATE TABLE __outpost_guard(id INTEGER PRIMARY KEY,valid INTEGER NOT NULL CHECK(valid=1))'),
      db.prepare('CREATE TABLE __outpost_batches(id TEXT PRIMARY KEY,hash TEXT NOT NULL)'),
      db.prepare('CREATE TABLE __outpost_operations(id TEXT PRIMARY KEY,hash TEXT NOT NULL,revision INTEGER NOT NULL)')];
    stmts.push(...data.schema.map(s=>db.prepare(s.sql)));
    stmts.push(bind(db,'INSERT INTO __outpost_meta VALUES(1,?,?,?,?,?,?,NULL)',[app,data.source_sha256,schemaHash,'migrating',0,JSON.stringify({schema:data.schema,backup:data.backup,sequences:data.sequences||{}})]));
    await transaction(db,stmts);return json({phase:'migrating',revision:0});
  }
  requireValue(meta && meta.app_id===app,'Catalog has not been initialized',404);
  if(action==='state')return json({app_id:app,phase:meta.phase,revision:meta.revision,source_sha256:meta.source_sha256,verification_sha256:meta.verification_sha256});
  if(action==='schema')return json({app_id:app,schema:JSON.parse(meta.manifest_json).schema});
  if(action==='import') {
    requireValue(meta.phase==='migrating','Catalog is already active',409);
    const data=await body(request,4*1024*1024);const entry=table(meta,data.table);const names=columns(entry,data.columns);
    requireValue(typeof data.batch_id==='string' && /^[A-Za-z0-9:_-]{1,180}$/.test(data.batch_id) && ['insert','self-links'].includes(data.mode),'Invalid import batch');
    requireValue(Array.isArray(data.rows) && data.rows.length>0 && data.rows.length<=500,'Invalid import row count');
    const digest=await hash(data);const previous=await bind(db,'SELECT hash FROM __outpost_batches WHERE id=?',[data.batch_id]).first();
    if(previous){requireValue(previous.hash===digest,'Import batch identity conflict',409);return json({imported:data.rows.length,replayed:true});}
    const stmts=[db.prepare('PRAGMA defer_foreign_keys=ON'),guard(db,"(SELECT phase FROM __outpost_meta WHERE id=1)='migrating'")];
    for(const row of data.rows) {
      requireValue(Array.isArray(row) && row.length===data.columns.length+1 && Number.isSafeInteger(row[0]),'Invalid import row');
      if(data.mode==='insert')stmts.push(bind(db,`INSERT INTO ${identifier(data.table)} (rowid,${names}) VALUES(${row.map(()=>'?').join(',')})`,row));
      else stmts.push(bind(db,`UPDATE ${identifier(data.table)} SET ${data.columns.map(c=>identifier(c)+'=?').join(',')} WHERE rowid=?`,[...row.slice(1),row[0]]));
    }
    stmts.push(bind(db,'INSERT INTO __outpost_batches VALUES(?,?)',[data.batch_id,digest]));
    await transaction(db,stmts);return json({imported:data.rows.length,replayed:false});
  }
  if(action==='scan') {
    const data=await body(request,65536);const entry=table(meta,data.table);
    requireValue(Number.isSafeInteger(data.start) && Number.isSafeInteger(data.end) && data.end>=data.start,'Invalid row range');
    const names=entry.columns.map(c=>c.name);const rows=await all(db,`SELECT rowid AS __outpost_rowid__,${columns(entry,names)} FROM ${identifier(data.table)} WHERE rowid>=? AND rowid<=? ORDER BY rowid LIMIT 501`,[data.start,data.end]);
    requireValue(rows.length<=500,'Scan exceeds its bounded size',413);
    return json({rows:rows.map(r=>[r.__outpost_rowid__,...names.map(c=>wire(r[c]))])});
  }
  if(action==='finalize') {
    const data=await body(request,65536);requireValue(/^[a-f0-9]{64}$/.test(data.verification_sha256),'Full verification receipt required');
    if(meta.phase==='ready'){requireValue(meta.verification_sha256===data.verification_sha256,'Finalization conflicts',409);return json({phase:'ready',revision:meta.revision});}
    const entries=JSON.parse(meta.manifest_json).schema.filter(s=>s.type==='table');
    requireValue(data.counts && Object.keys(data.counts).length===entries.length,'All table counts required');
    const predicates=[];const values=[];
    for(const s of entries){requireValue(Number.isSafeInteger(data.counts[s.name])&&data.counts[s.name]>=0,'Invalid table count');predicates.push(`(SELECT COUNT(*) FROM ${identifier(s.name)})=?`);values.push(data.counts[s.name]);}
    requireValue((await all(db,'PRAGMA foreign_key_check')).length===0,'Cloud foreign-key verification failed',409);
    const sequenceStatements=[];
    for(const [name,value] of Object.entries(JSON.parse(meta.manifest_json).sequences||{})) {
      sequenceStatements.push(bind(db,'DELETE FROM sqlite_sequence WHERE name=?',[name]),bind(db,'INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',[name,value]));
    }
    await transaction(db,[guard(db,predicates.join(' AND '),values),...sequenceStatements,bind(db,"UPDATE __outpost_meta SET phase='ready',verification_sha256=? WHERE id=1",[data.verification_sha256])]);
    return json({phase:'ready',revision:0});
  }
  requireValue(meta.phase==='ready','Catalog migration is not yet verified',409);
  if(action==='rows') {
    const data=await body(request,65536);const entry=table(meta,data.table,true);const filters=data.filters||{};
    requireValue(filters && typeof filters==='object'&&!Array.isArray(filters),'Invalid filters');
    const keys=Object.keys(filters);if(keys.length)columns(entry,keys);
    const limit=data.limit??100,offset=data.offset??0;
    requireValue(Number.isSafeInteger(limit)&&limit>0&&limit<=500&&Number.isSafeInteger(offset)&&offset>=0,'Invalid pagination');
    const where=keys.length?' WHERE '+keys.map(k=>identifier(k)+' IS ?').join(' AND '):'';
    const primary=entry.columns.filter(c=>c.pk).sort((a,b)=>a.pk-b.pk).map(c=>identifier(c.name));
    const order=primary.length?' ORDER BY '+primary.join(','):'';
    let rows;
    try {rows=await all(db,`SELECT * FROM ${identifier(data.table)}${where}${order} LIMIT ? OFFSET ?`,[...keys.map(k=>filters[k]),limit,offset]);}
    catch(error) {if(String(error).includes('size'))throw new CatalogError(413,'Query page is too large');throw error;}
    return json({rows:rows.map(r=>Object.fromEntries(Object.entries(r).map(([k,v])=>[k,wire(v)]))),revision:meta.revision});
  }
  if(action==='sql') {
    const data=await body(request,200000);const sql=typeof data.sql==='string'?data.sql.trim().replace(/;$/,''):'';
    requireValue(/^SELECT\b/i.test(sql)&&!sql.includes(';')&&!sql.toLowerCase().includes(internal)&&encoder.encode(sql).length<=100000,'Read queries accept one SELECT statement');
    requireValue(Array.isArray(data.params||[])&&(data.params||[]).length<=100,'Invalid query parameters');
    const rows=await all(db,sql,data.params||[]);
    return json({rows:rows.map(r=>Object.fromEntries(Object.entries(r).map(([k,v])=>[k,wire(v)]))),revision:meta.revision});
  }
  if(action==='mutate') {
    const data=await body(request);operation(data);
    const digest=await hash({expected_revision:data.expected_revision,statements:data.statements});
    const prior=await bind(db,'SELECT hash,revision FROM __outpost_operations WHERE id=?',[data.operation_id]).first();
    if(prior){requireValue(prior.hash===digest,'Operation identity conflict',409);return json({revision:prior.revision,replayed:true,dry_run:Boolean(data.dry_run)});}
    requireValue(meta.revision===data.expected_revision,'Catalog changed; read the new revision before retrying',409);
    if(data.dry_run)return json({dry_run:true,revision:meta.revision,statements:data.statements.length});
    await transaction(db,[db.prepare('PRAGMA defer_foreign_keys=ON'),guard(db,'(SELECT revision FROM __outpost_meta WHERE id=1)=?',[data.expected_revision]),
      ...data.statements.map(s=>bind(db,s.sql,s.params)),db.prepare('UPDATE __outpost_meta SET revision=revision+1 WHERE id=1'),
      bind(db,'INSERT INTO __outpost_operations VALUES(?,?,?)',[data.operation_id,digest,data.expected_revision+1])]);
    return json({revision:data.expected_revision+1,replayed:false,dry_run:false});
  }
  throw new CatalogError(404,'Catalog route not found');
}
