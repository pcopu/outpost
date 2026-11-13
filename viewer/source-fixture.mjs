import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import worker from './worker.mjs';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
function stable(v) {return v && typeof v === 'object' ? Array.isArray(v) ? v.map(stable) : Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])) : v;}
export function fixture(t) {
  const sql = new DatabaseSync(':memory:'); t.after(()=>sql.close());
  sql.exec(`CREATE TABLE __outpost_meta(id INTEGER PRIMARY KEY,app_id TEXT,phase TEXT,revision INTEGER);
    INSERT INTO __outpost_meta VALUES(1,'demo','ready',0);
    CREATE TABLE __outpost_guard(id INTEGER PRIMARY KEY,valid INTEGER CHECK(valid=1));
    CREATE TABLE __outpost_operations(id TEXT PRIMARY KEY,hash TEXT,revision INTEGER);`);
  function prepare(query, values=[]) {return {bind(...v){return prepare(query,v);}, async first(){return sql.prepare(query).get(...values)??null;}, async all(){return {results:sql.prepare(query).all(...values)};}, query, values};}
  const db={prepare,async batch(items){sql.exec('BEGIN');try{const result=items.map(s=>({results:s.query.trim().startsWith('SELECT')?sql.prepare(s.query).all(...s.values):[],meta:s.query.trim().startsWith('SELECT')?{}:sql.prepare(s.query).run(...s.values)}));sql.exec('COMMIT');return result;}catch(e){sql.exec('ROLLBACK');throw e;}}};
  const objects=new Map();
  objects.set('viewer/index.json',Buffer.from('{"projects":[{"id":"demo"}]}'));
  const env={ADMIN_TOKEN:'private',READ_KEY:'read',SESSION_SECRET:'session',CATALOG_DATABASES:'{"demo":"DB"}',DB:db,
    MEDIA:{async get(key){const b=objects.get(key);return b ? {body:new Blob([b]).stream(),size:b.length,async json(){return JSON.parse(b);},async arrayBuffer(){return b;}}:null;},
      async head(key){const b=objects.get(key);return b?{size:b.length}:null;},
      async put(key,body,options){const b=Buffer.from(await new Response(body).arrayBuffer());if(options.sha256)assert.equal(hash(b),Buffer.from(options.sha256).toString('hex'));if(objects.has(key)&&options.onlyIf)return null;objects.set(key,b);return {};}}};
  async function call(route, data, {token='private',method,raw,cookie,app='demo'}={}) {
    return worker.fetch(new Request(`https://outpost.example/api/sources/${app}/${route}`,{method:method||(data?'POST':'GET'),headers:{...(token?{Authorization:'Bearer '+token}:{}),...(cookie?{Cookie:cookie}:{}),...(raw?{'Content-Length':String(raw.length)}:{})},body:raw|| (data?JSON.stringify(data):undefined)}),env);
  }
  const bytes=Buffer.from('original narration');
  const file={path:'audio/voice.wav',role:'audio',language:'en',sha256:hash(bytes),bytes:bytes.length,locator:{kind:'chunks',chunks:[{sha256:hash(bytes),bytes:bytes.length}]}};
  const manifest={app_id:'demo',video_id:'production:film',files:[file],dependencies:[{name:'narration',status:'present',path:file.path,sha256:file.sha256}],recipe:{entrypoint:null,command:[],tools:{}},git:{repository:null,commit:null,subdirectory:null,working_tree:'unknown',source_tree_sha256:hash(JSON.stringify(stable([{path:file.path,sha256:file.sha256,bytes:file.bytes}])))},lineage:[]};
  const revision={format_version:1,revision_id:hash(JSON.stringify(stable(manifest))),label:'First cut',manifest};
  async function upload(){const r=await call('blob/'+hash(bytes),undefined,{method:'PUT',raw:bytes});assert.equal(r.status,201,await r.text());}
  return {call,env,revision,file,bytes,objects,upload,sql};
}
