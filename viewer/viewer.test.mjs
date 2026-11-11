import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
let worker;
try { worker = (await import('./worker.mjs')).default; } catch {}
function bucket(data = {}) {
  const entries = new Map(Object.entries(data).map(([key, value]) => [key, {bytes: new TextEncoder().encode(value), etag: 'one'}]));
  return {
    async get(key, options) {
      const object = entries.get(key);
      if (!object) return null;
      const bytes = options?.range ? object.bytes.slice(options.range.offset, options.range.offset + options.range.length) : object.bytes;
      return {etag: object.etag, body: new Blob([bytes]).stream(), json: async () => JSON.parse(new TextDecoder().decode(bytes))};
    },
    async put(key, value, options) {
      const old = entries.get(key);
      if (options?.onlyIf?.etagMatches && old?.etag !== options.onlyIf.etagMatches) return null;
      if (options?.onlyIf?.etagDoesNotMatch === '*' && old) return null;
      entries.set(key, {bytes: new TextEncoder().encode(value), etag: crypto.randomUUID()});
      return {etag: entries.get(key).etag};
    }
  };
}
function fixture() {
  const sha = 'a'.repeat(64);
  const project = {id: 'alpha', name: 'Alpha', videos: [{id: 'film', title: 'A film', files: [{sha256: sha, role: 'render'}]}], posts: [], assets: [{sha256: sha, bytes: 10, mime: 'video/mp4', name: 'film.mp4', location: {binding: 'ARCHIVE', key: 'private/alpha/film'}}]};
  return {sha, env: {READ_KEY: 'test-read', SESSION_SECRET: 'test-session', ADMIN_TOKEN: 'test-admin', MEDIA: bucket({'viewer/index.json': JSON.stringify({projects: [{id: 'alpha', name: 'Alpha'}]}), 'viewer/projects/alpha.json': JSON.stringify(project)}), ARCHIVE: bucket({'private/alpha/film': '0123456789'}), ASSETS: {fetch: async () => new Response('Outpost')}}};
}
async function call(env, path, init = {}) {return worker.fetch(new Request('https://viewer.example'+path, init), env);}
async function login(env) {
  const r = await call(env, '/api/session', {method: 'POST', body: JSON.stringify({key: 'test-read'})});
  assert.equal(r.status, 200);
  return r.headers.get('set-cookie').split(';')[0];
}
test('viewer requires authentication before exposing project or media data', async () => {
  assert.ok(worker, 'Viewer Worker is implemented');
  const {env, sha} = fixture();
  for (const path of ['/api/projects', '/api/projects/alpha', '/api/projects/alpha/assets/'+sha]) assert.equal((await call(env, path)).status, 401);
  assert.equal((await call(env, '/api/session', {method: 'POST', body: JSON.stringify({key: 'bad'})})).status, 401);
});
test('viewer isolates projects and streams byte ranges from private R2', async () => {
  assert.ok(worker, 'Viewer Worker is implemented');
  const {env, sha} = fixture();const cookie = await login(env);
  assert.equal((await call(env, '/api/projects/beta', {headers: {cookie}})).status, 404);
  const r = await call(env, '/api/projects/alpha/assets/'+sha, {headers: {cookie, range: 'bytes=3-6'}});
  assert.equal(r.status, 206);assert.equal(await r.text(), '3456');assert.equal(r.headers.get('content-range'), 'bytes 3-6/10');
  assert.equal((await call(env, '/api/projects/alpha/assets/'+sha, {headers: {cookie, range: 'bytes=20-'}})).status, 416);
  assert.equal((await call(env, '/api/projects/alpha/assets/'+'b'.repeat(64), {headers: {cookie}})).status, 404);
});
test('manual posting observations are append-only and retries are idempotent', async () => {
  assert.ok(worker, 'Viewer Worker is implemented');
  const {env} = fixture();
  const event = {id: crypto.randomUUID(), videoId: 'film', postId: 'post-one', platform: 'youtube', status: 'published', url: 'https://youtube.com/watch?v=example', postedAt: '2026-01-01T12:00:00Z', observedAt: '2026-01-02T12:00:00Z', source: 'Reviewed publication receipt', views: 0};
  const send = (data, headers = {authorization: 'Bearer test-admin'}) => call(env, '/api/projects/alpha/events', {method: 'POST', headers, body: JSON.stringify(data)});
  assert.equal((await send(event, {})).status, 401);
  assert.equal((await send(event)).status, 201);assert.equal((await send(event)).status, 200);
  assert.equal((await send({...event, views: 1})).status, 409);
  assert.equal((await send({...event, id: crypto.randomUUID(), videoId: 'missing'})).status, 400);
  assert.equal((await send({...event, id: crypto.randomUUID(), views: -1})).status, 400);
  assert.equal((await send({...event, id: crypto.randomUUID(), url: 'javascript:alert(1)'})).status, 400);
  const cookie = await login(env);const p = await (await call(env, '/api/projects/alpha', {headers: {cookie}})).json();
  assert.equal(p.observations.length, 1);assert.equal(p.observations[0].views, 0);
});
test('cross-origin writes are rejected and source downloads are sandboxed', async () => {
  assert.ok(worker, 'Viewer Worker is implemented');
  const {env, sha} = fixture();
  const r = await call(env, '/api/session', {method: 'POST', headers: {origin: 'https://other.example'}, body: JSON.stringify({key: 'test-read'})});
  assert.equal(r.status, 403);
  const cookie = await login(env);const download = await call(env, '/api/projects/alpha/assets/'+sha+'?download=1', {headers: {cookie}});
  assert.match(download.headers.get('content-disposition'), /^attachment/);assert.equal(download.headers.get('x-content-type-options'), 'nosniff');
});
test('packed sources stream correctly across compressed chunk boundaries', async () => {
  const {gzipSync} = await import('node:zlib');
  const {env, sha} = fixture();
  const chunks = new Map([['chunk/one', gzipSync('prefixABCDE')], ['chunk/two', gzipSync('FGHIJsuffix')]]);
  const original = env.MEDIA.get.bind(env.MEDIA);
  env.MEDIA.get = async (key, options) => {
    if (chunks.has(key)) return {body: new Blob([chunks.get(key)]).stream()};
    if (key === 'viewer/projects/alpha.json') return {json: async () => ({id: 'alpha', videos: [], assets: [{sha256: sha, bytes: 10, mime: 'text/plain', name: 'script.txt', location: {binding: 'MEDIA', parts: [{key: 'chunk/one', offset: 6, length: 5, encoding: 'gzip'}, {key: 'chunk/two', offset: 0, length: 5, encoding: 'gzip'}]}}]})};
    return original(key, options);
  };
  const cookie = await login(env);
  const response = await call(env, '/api/projects/alpha/assets/'+sha, {headers: {cookie, range: 'bytes=3-7'}});
  assert.equal(response.status, 206);assert.equal(await response.text(), 'DEFGH');
});
test('published observations preserve a verified date when the exact time is unknown', async () => {
  const {env} = fixture();
  const event = {id: crypto.randomUUID(), videoId: 'film', postId: 'date-only', platform: 'youtube', status: 'published', url: 'https://youtu.be/example', postedDate: '2026-01-01', observedAt: '2026-01-02T12:00:00Z', source: 'Dated publication receipt'};
  const send = data => call(env, '/api/projects/alpha/events', {method: 'POST', headers: {authorization: 'Bearer test-admin'}, body: JSON.stringify(data)});
  const response = await send(event);
  assert.equal(response.status, 201);assert.equal((await response.json()).postedDate, '2026-01-01');
  assert.equal((await send({...event, id: crypto.randomUUID(), postedDate: '2026-02-31'})).status, 400);
});
