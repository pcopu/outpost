import test from 'node:test';
import assert from 'node:assert/strict';
import {fixture} from './source-fixture.mjs';
import {createHash} from 'node:crypto';
import worker from './worker.mjs';

function stable(v) {return v && typeof v === 'object' ? Array.isArray(v) ? v.map(stable) : Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])) : v;}
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

test('sources publish only after upload; dry runs do not create tables; idempotency and CAS preserve versions',async t=>{
  const {call,revision,upload,sql}=fixture(t);
  const data={revision,expected_revision:0,operation_id:'source-one'};
  let r=await call('publish',{...data,dry_run:true});assert.equal(r.status,200,await r.text());
  assert.equal(sql.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE name='__outpost_sources'").get().n,0);
  assert.equal((await call('publish',data)).status,409);
  await upload();r=await call('publish',data);assert.equal(r.status,201,await r.text());
  assert.equal((await call('publish',data)).status,200);
  assert.equal((await call('publish',{...data,operation_id:'stale'})).status,409);
  r=await call('revision/'+revision.revision_id);assert.deepEqual(await r.json(),revision);
});

test('readers can obtain exact sources but cannot publish or access arbitrary chunks',async t=>{
  const {call,env,revision,upload,file,bytes}=fixture(t);await upload();
  await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const session=await worker.fetch(new Request('https://outpost.example/api/session',{method:'POST',body:JSON.stringify({key:'read'})}),env);
  const cookie=session.headers.get('Set-Cookie').split(';')[0];
  assert.equal((await call('publish',{revision,expected_revision:1,operation_id:'two'},{token:'',cookie})).status,401);
  assert.equal((await call('blob/'+file.sha256,undefined,{token:'',cookie})).status,401);
  const r=await call('revision/'+revision.revision_id+'/files/0',undefined,{token:'',cookie});
  assert.equal(r.status,200,await r.clone().text());assert.deepEqual(Buffer.from(await r.arrayBuffer()),bytes);
  assert.equal((await call('revision/'+revision.revision_id,undefined,{app:'other'})).status,404);
});

test('source identity, unsafe filenames, and mismatched dependency claims are rejected',async t=>{
  const {call,revision}=fixture(t);
  for(const mutate of [m=>m.app_id='other',m=>m.files[0].path='../outside',m=>m.files[0].path='CON.txt',m=>m.dependencies[0].sha256='a'.repeat(64)]){
    const changed=structuredClone(revision);mutate(changed.manifest);changed.revision_id=hash(JSON.stringify(stable(changed.manifest)));
    assert.equal((await call('publish',{revision:changed,expected_revision:0,operation_id:'invalid',dry_run:true})).status,400);
  }
});

test('render records require an exact same-video revision and preserve historical uncertainty',async t=>{
  const {call,revision,upload,file}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const render={app_id:'demo',video_id:'production:film',source_revision_id:revision.revision_id,output:{...file,role:'render'},settings:{aspect_ratio:'9:16',language:'en'},association:{status:'unverified',evidence:'Historical import; no rendering receipt'}};
  let r=await call('render',{render,expected_revision:1,operation_id:'render-one'});assert.equal(r.status,201,await r.text());
  assert.equal((await call('render',{render,expected_revision:1,operation_id:'render-one'})).status,200);
  assert.equal((await call('render',{render:{...render,video_id:'other'},expected_revision:2,operation_id:'wrong-video'})).status,409);
});

test('bounded project package contains only one revision and verifies bytes before download',async t=>{
  const {call,revision,upload,objects,file}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const r=await call('revision/'+revision.revision_id+'/package');assert.equal(r.status,200,await r.clone().text());
  const bytes=Buffer.from(await r.arrayBuffer());assert.equal(bytes.readUInt32LE(0),0x04034b50);assert.ok(bytes.includes(Buffer.from('audio/voice.wav')));assert.ok(bytes.includes(Buffer.from('outpost-source.json')));
  objects.set('sources/demo/objects/'+file.sha256,Buffer.from('corrupt'));
  assert.equal((await call('revision/'+revision.revision_id+'/package')).status,409);
});

test('listing sources reflects current D1 writes and never mixes videos',async t=>{
  const {call,revision,upload}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  let result=await (await call('list?video=production%3Afilm')).json();assert.equal(result.revisions.length,1);assert.equal(result.catalog_revision,1);
  result=await (await call('list?video=other')).json();assert.equal(result.revisions.length,0);
});

test('viewer source routes honor the project exposure allowlist',async t=>{
  const {call,env,revision,upload,objects}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const session=await worker.fetch(new Request('https://outpost.example/api/session',{method:'POST',body:JSON.stringify({key:'read'})}),env);
  const cookie=session.headers.get('Set-Cookie').split(';')[0];objects.set('viewer/index.json',Buffer.from('{"projects":[]}'));
  for(const route of ['list?video=production%3Afilm','revision/'+revision.revision_id,'revision/'+revision.revision_id+'/files/0'])assert.equal((await call(route,undefined,{token:'',cookie})).status,404);
  assert.equal((await call('revision/'+revision.revision_id)).status,200);
});

test('restore/render/approval evidence is append-only and separate from file verification',async t=>{
  const {call,revision,upload}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const check={app_id:'demo',video_id:'production:film',source_revision_id:revision.revision_id,kind:'restore',status:'passed',observed_at:'2026-01-01T00:00:00Z',environment:{os:'test-os',tools:{python:'3.12'}},evidence:'Fresh directory, hashes verified; no render performed'};
  let r=await call('check',{check,expected_revision:1,operation_id:'restore-check'});assert.equal(r.status,201,await r.text());
  r=await call('list?video=production%3Afilm');const list=await r.json();assert.equal(list.checks.length,1);assert.equal(list.checks[0].kind,'restore');
  assert.equal((await call('check',{check:{...check,video_id:'other'},expected_revision:2,operation_id:'wrong'})).status,409);
});

test('new versions and aspect variants remain distinct while retries do not duplicate them',async t=>{
  const {call,revision,upload,file}=fixture(t);await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const second=structuredClone(revision);second.manifest.recipe.command=['new-recipe'];second.revision_id=hash(JSON.stringify(stable(second.manifest)));second.label='Second cut';
  assert.equal((await call('publish',{revision:second,expected_revision:1,operation_id:'two'})).status,201);
  for(const [index,aspect] of ['16:9','9:16'].entries()) {
    const render={app_id:'demo',video_id:'production:film',source_revision_id:revision.revision_id,output:{...file,role:'render'},settings:{aspect_ratio:aspect,language:'en'},association:{status:'unverified',evidence:'Historic'}};
    assert.equal((await call('render',{render,expected_revision:index+2,operation_id:'aspect-'+index})).status,201);
  }
  const list=await (await call('list?video=production%3Afilm')).json();assert.equal(list.revisions.length,2);assert.equal(list.renders.length,2);assert.ok(list.renders.every(r=>r.source_revision_id===revision.revision_id));
});

test('historical snapshot locators preserve original bytes without repacking and reject mismatched manifests',async t=>{
  const {call,revision,objects,bytes,file}=fixture(t),snapshot='b'.repeat(64),base='apps/demo/snapshots/'+snapshot;
  const manifest={app_id:'demo',snapshot_id:snapshot,entries:[{kind:'file',path:'files/original.zip',sha256:file.sha256,size:file.bytes}]};
  const raw=Buffer.from(JSON.stringify(manifest));objects.set(base+'/manifest.json',raw);objects.set(base+'/files/original.zip',bytes);
  objects.set(base+'/COMMITTED.json',Buffer.from(JSON.stringify({format_version:1,app_id:'demo',snapshot_id:snapshot,manifest_sha256:hash(raw)})));
  const historic=structuredClone(revision);historic.manifest.files[0].locator={kind:'snapshot',snapshot_id:snapshot,source_path:'files/original.zip'};historic.revision_id=hash(JSON.stringify(stable(historic.manifest)));
  let r=await call('publish',{revision:historic,expected_revision:0,operation_id:'archive'});assert.equal(r.status,201,await r.text());
  r=await call('revision/'+historic.revision_id+'/files/0');assert.equal(r.status,200);assert.deepEqual(Buffer.from(await r.arrayBuffer()),bytes);
  objects.set(base+'/manifest.json',Buffer.from('{}'));
  assert.equal((await call('revision/'+historic.revision_id+'/files/0')).status,409);
});

test('corrupt chunk upload and operation identity reuse cannot replace a source revision',async t=>{
  const {call,revision,upload,file}=fixture(t);
  assert.equal((await call('blob/'+file.sha256,undefined,{method:'PUT',raw:Buffer.from('wrong')})).status,400);
  await upload();await call('publish',{revision,expected_revision:0,operation_id:'one'});
  const changed=structuredClone(revision);changed.manifest.recipe.command=['changed'];changed.revision_id=hash(JSON.stringify(stable(changed.manifest)));
  assert.equal((await call('publish',{revision:changed,expected_revision:0,operation_id:'one'})).status,409);
  assert.equal((await call('revision/'+changed.revision_id)).status,404);
});

test('publication validates a large chunk set through registered receipts instead of repeated R2 reads',async t=>{
  const {call,revision,upload,env,file}=fixture(t);await upload();
  let heads=0;const original=env.MEDIA.head;env.MEDIA.head=async key=>{heads++;return original(key);};
  const large=structuredClone(revision);
  large.manifest.files=Array.from({length:80},(_,i)=>({...file,path:`inputs/file-${i}.wav`}));large.manifest.dependencies=[];
  large.manifest.git.source_tree_sha256=hash(JSON.stringify(stable(large.manifest.files.map(f=>({path:f.path,bytes:f.bytes,sha256:f.sha256})))));
  large.revision_id=hash(JSON.stringify(stable(large.manifest)));
  const response=await call('publish',{revision:large,expected_revision:0,operation_id:'many-files'});assert.equal(response.status,201,await response.text());
  assert.equal(heads,0,'Source commit should validate app-scoped upload receipts in one bounded D1 query');
});
