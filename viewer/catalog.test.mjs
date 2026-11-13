import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import worker from './worker.mjs';

// Real SQLite executes the statements and transaction failures. The small
// adapter supplies the D1 binding shape; live Worker checks cover its runtime.
function database() {
  const db = new DatabaseSync(':memory:'); db.exec('PRAGMA foreign_keys=ON');
  function statement(sql, values = []) {
    return {sql, values, bind(...args) {return statement(sql, args);},
      async all() {return {results: db.prepare(sql).all(...values), success: true};},
      async first() {return db.prepare(sql).get(...values) ?? null;},
      async run() {return {meta: db.prepare(sql).run(...values), success: true};}};
  }
  return {prepare: statement, async batch(statements) {
    db.exec('BEGIN');
    try {
      const result = [];
      for (const s of statements) result.push(await s.run());
      db.exec('COMMIT'); return result;
    } catch (e) {db.exec('ROLLBACK'); throw e;}
  }, close() {db.close();}};
}
function fixture(t) {
  const db = database();t.after(() => db.close());
  const objects = new Map();
  const env = {ADMIN_TOKEN: 'private-admin', READ_KEY: 'read', SESSION_SECRET: 'session',
    CATALOG_DATABASES: JSON.stringify({demo: 'DEMO_DB'}), DEMO_DB: db,
    MEDIA: {async get(key) {const bytes=objects.get(key);return bytes ? {body:new Blob([bytes]).stream(),size:bytes.length} : null;},
      async head(key) {const bytes=objects.get(key);return bytes ? {size:bytes.length} : null;},
      async put(key, value) {const bytes=new Uint8Array(await new Response(value).arrayBuffer());objects.set(key,bytes);return {};}}};
  async function call(route, body, token='private-admin') {
    return worker.fetch(new Request('https://outpost.example/api/catalog/demo/'+route, {
      method: body === undefined ? 'GET' : 'POST', headers: token ? {Authorization:'Bearer '+token} : {},
      body: body === undefined ? undefined : JSON.stringify(body)}), env);
  }
  const schema = [
    {type:'table',name:'accounts',sql:'CREATE TABLE accounts(handle TEXT PRIMARY KEY, kind TEXT NOT NULL)',columns:[{name:'handle',type:'TEXT',pk:1},{name:'kind',type:'TEXT',pk:0}]},
    {type:'table',name:'videos',sql:'CREATE TABLE videos(id TEXT PRIMARY KEY, account TEXT REFERENCES accounts(handle), title TEXT)',columns:[{name:'id',type:'TEXT',pk:1},{name:'account',type:'TEXT',pk:0},{name:'title',type:'TEXT',pk:0}]}
  ];
  async function init() {
    const r=await call('init',{app_id:'demo',source_sha256:'a'.repeat(64),schema,
      backup:{snapshot_id:'b'.repeat(64),catalog_sha256:'a'.repeat(64)}});
    assert.equal(r.status,200,await r.text());
  }
  return {call,env,init,schema};
}

test('cloud catalog routes require admin authentication and isolate app bindings', async t => {
  const {call,env}=fixture(t);
  assert.equal((await call('state',undefined,'')).status,401);
  assert.equal((await call('state',undefined,'wrong')).status,401);
  const r=await worker.fetch(new Request('https://outpost.example/api/catalog/other/state',{headers:{Authorization:'Bearer private-admin'}}),env);
  assert.equal(r.status,404);
});

test('migration batches are retryable, conflicting retries fail, normal reads wait for activation', async t => {
  const {call,init}=fixture(t);await init();
  const body={table:'accounts',batch_id:'accounts-1',columns:['handle','kind'],rows:[[1,'one','brand']],mode:'insert'};
  assert.equal((await call('import',body)).status,200);
  assert.equal((await call('import',body)).status,200);
  assert.equal((await call('import',{...body,rows:[[1,'changed','brand']]})).status,409);
  assert.equal((await call('rows',{table:'accounts',limit:10})).status,409);
  const scanned=await call('scan',{table:'accounts',start:1,end:1});assert.equal(scanned.status,200);
  assert.deepEqual((await scanned.json()).rows,[[1,'one','brand']]);
  assert.equal((await call('finalize',{counts:{accounts:1,videos:0},verification_sha256:'c'.repeat(64)})).status,200);
  const rows=await call('rows',{table:'accounts',limit:10});assert.equal(rows.status,200);
  assert.deepEqual((await rows.json()).rows,[{handle:'one',kind:'brand'}]);
  assert.equal((await call('import',{...body,batch_id:'accounts-2',rows:[[2,'two','brand']]})).status,409);
});

test('atomic writes preserve foreign keys, revision conflicts and retry idempotency', async t => {
  const {call,init}=fixture(t);await init();
  await call('import',{table:'accounts',batch_id:'accounts-1',columns:['handle','kind'],rows:[[1,'one','brand']],mode:'insert'});
  await call('finalize',{counts:{accounts:1,videos:0},verification_sha256:'c'.repeat(64)});
  const body={operation_id:'op-one',expected_revision:0,statements:[{sql:'INSERT INTO videos VALUES (?,?,?)',params:['film','one','Title']}]};
  assert.equal((await call('mutate',{...body,dry_run:true})).status,200);
  assert.equal((await (await call('rows',{table:'videos',limit:10})).json()).rows.length,0);
  assert.equal((await call('mutate',body)).status,200);
  assert.equal((await call('mutate',body)).status,200);
  assert.equal((await call('mutate',{...body,operation_id:'stale'})).status,409);
  const invalid={operation_id:'bad-fk',expected_revision:1,statements:[
    {sql:'INSERT INTO accounts VALUES (?,?)',params:['two','brand']},
    {sql:'INSERT INTO videos VALUES (?,?,?)',params:['invalid','missing','Title']}]};
  assert.equal((await call('mutate',invalid)).status,409);
  assert.equal((await (await call('rows',{table:'accounts',limit:10})).json()).rows.length,1);
  const state=await (await call('state')).json();assert.equal(state.revision,1);
});

test('read SQL cannot mutate, internal tables cannot be written, and errors do not echo values', async t => {
  const {call,init}=fixture(t);await init();
  await call('finalize',{counts:{accounts:0,videos:0},verification_sha256:'c'.repeat(64)});
  assert.equal((await call('sql',{sql:'DELETE FROM accounts'})).status,400);
  assert.equal((await call('sql',{sql:'SELECT 1; DELETE FROM accounts'})).status,400);
  assert.equal((await call('mutate',{operation_id:'internal',expected_revision:0,statements:[{sql:'DELETE FROM __outpost_meta',params:[]}]})).status,400);
  const secret='private-value-never-in-error';
  const r=await call('mutate',{operation_id:'bad',expected_revision:0,statements:[{sql:'INSERT INTO videos VALUES (?,?,?)',params:['film','missing',secret]}]});
  assert.equal(r.status,409);assert.ok(!(await r.text()).includes(secret));
  const sql=await call('sql',{sql:'SELECT COUNT(*) AS n FROM accounts',params:[]});
  assert.equal(sql.status,200);assert.deepEqual((await sql.json()).rows,[{n:0}]);
});

test('migration preserves autoincrement counters after deleted historical rows', async t => {
  const {call}=fixture(t);
  const schema=[{type:'table',name:'events',sql:'CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)',
    columns:[{name:'id',type:'INTEGER',pk:1},{name:'note',type:'TEXT',pk:0}]}];
  const init=await call('init',{app_id:'demo',source_sha256:'a'.repeat(64),schema,
    sequences:{events:50},backup:{snapshot_id:'b'.repeat(64),catalog_sha256:'a'.repeat(64)}});
  assert.equal(init.status,200);
  assert.equal((await call('finalize',{counts:{events:0},verification_sha256:'c'.repeat(64)})).status,200);
  assert.equal((await call('mutate',{operation_id:'next-id',expected_revision:0,
    statements:[{sql:'INSERT INTO events(note) VALUES (?)',params:['next']}]})).status,200);
  const rows=await (await call('rows',{table:'events',limit:10})).json();
  assert.equal(rows.rows[0].id,51);
});
