import {handleCatalog, CatalogError} from './catalog.mjs';
import {handleSources, sourceVideos} from './sources.mjs';
const encoder = new TextEncoder();
const security = {
  'Cache-Control': 'private, no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
};
class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const json = (data, status = 200, headers = {}) => Response.json(data, {status, headers: {...security, ...headers}});
const encode = bytes => btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
const decode = value => Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
async function signingKey(env) {
  return crypto.subtle.importKey('raw', encoder.encode(env.SESSION_SECRET), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign', 'verify']);
}
async function ticket(env, kind, seconds) {
  const payload = encode(encoder.encode(JSON.stringify({kind, expires: Date.now() + seconds * 1000, nonce: crypto.randomUUID()})));
  const signature = await crypto.subtle.sign('HMAC', await signingKey(env), encoder.encode(payload));
  return payload + '.' + encode(new Uint8Array(signature));
}
async function validTicket(env, value, kind) {
  try {
    const [payload, signature, extra] = value.split('.');
    if (!payload || !signature || extra) return false;
    const data = JSON.parse(new TextDecoder().decode(decode(payload)));
    return data.kind === kind && data.expires > Date.now() && await crypto.subtle.verify('HMAC', await signingKey(env), decode(signature), encoder.encode(payload));
  } catch { return false; }
}
async function sameSecret(actual, expected) {
  if (typeof actual !== 'string' || !expected) return false;
  const hashes = await Promise.all([actual, expected].map(value => crypto.subtle.digest('SHA-256', encoder.encode(value))));
  const a = new Uint8Array(hashes[0]), b = new Uint8Array(hashes[1]);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}
async function admin(request, env) {
  return sameSecret(request.headers.get('Authorization')?.replace(/^Bearer /, ''), env.ADMIN_TOKEN);
}
async function authenticated(request, env) {
  const value = (request.headers.get('Cookie') || '').split(';').map(v => v.trim()).find(v => v.startsWith('outpost_session='))?.slice(16);
  return Boolean(value && await validTicket(env, value, 'session'));
}
async function readJson(request) {
  if (!request.body) throw new HttpError(400, 'JSON body required');
  const reader = request.body.getReader();
  const blocks = []; let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 65536) throw new HttpError(413, 'Request is too large');
      blocks.push(value);
    }
  } finally { await reader.cancel(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const block of blocks) { bytes.set(block, offset); offset += block.length; }
  try { return JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new HttpError(400, 'Invalid JSON'); }
}
async function project(env, id) {
  if (!/^[a-z][a-z0-9-]{0,63}$/.test(id)) throw new HttpError(404, 'Project not found');
  const index = await env.MEDIA.get('viewer/index.json');
  if (!index || !(await index.json()).projects.some(p => p.id === id)) throw new HttpError(404, 'Project not found');
  const object = await env.MEDIA.get(`viewer/projects/${id}.json`);
  if (!object) throw new HttpError(404, 'Project not found');
  const result = await object.json();
  if (result.id !== id) throw new Error('Project identity mismatch');
  return result;
}
async function ledger(env, id) {
  const object = await env.MEDIA.get(`viewer/observations/${id}.json`);
  return {events: object ? (await object.json()).events : [], etag: object?.etag};
}
function timestamp(value, name, required = false) {
  if (value == null && !required) return null;
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT.*(Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) throw new HttpError(400, `Invalid ${name}`);
  return value;
}
function validateEvent(input, catalog) {
  if (!/^[a-f0-9]{8}-[a-f0-9-]{27}$/.test(input.id) || !catalog.videos.some(v => v.id === input.videoId)) throw new HttpError(400, 'Unknown video or invalid event identity');
  const hosts = {youtube: ['youtube.com', 'youtu.be'], instagram: ['instagram.com'], tiktok: ['tiktok.com'], facebook: ['facebook.com', 'fb.watch'], other: []};
  if (!Object.hasOwn(hosts, input.platform)) throw new HttpError(400, 'Unknown platform');
  if (typeof input.postId !== 'string' || !input.postId.trim() || input.postId.length > 200) throw new HttpError(400, 'Post ID required');
  if (typeof input.source !== 'string' || !input.source.trim() || input.source.length > 1000) throw new HttpError(400, 'Evidence source required');
  if (!['published', 'scheduled', 'draft', 'removed', 'unknown'].includes(input.status)) throw new HttpError(400, 'Invalid status');
  let url = null;
  if (input.url != null) {
    try {
      const parsed = new URL(input.url);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || (hosts[input.platform].length && !hosts[input.platform].some(h => parsed.hostname === h || parsed.hostname.endsWith('.' + h)))) throw new Error();
      url = parsed.href;
    } catch { throw new HttpError(400, 'Invalid post URL'); }
  }
  const event = {id: input.id, videoId: input.videoId, postId: input.postId, platform: input.platform, status: input.status, url, postedAt: timestamp(input.postedAt, 'postedAt'), scheduledAt: timestamp(input.scheduledAt, 'scheduledAt'), observedAt: timestamp(input.observedAt, 'observedAt', true), source: input.source};
  event.postedDate = input.postedDate ?? null;
  if (event.postedDate !== null && (typeof event.postedDate !== 'string' || !/^\d{4}-\d\d-\d\d$/.test(event.postedDate) || !Number.isFinite(Date.parse(event.postedDate)) || new Date(event.postedDate).toISOString().slice(0, 10) !== event.postedDate)) throw new HttpError(400, 'Invalid publication date');
  if (event.status === 'published' && (!event.url || (!event.postedAt && !event.postedDate))) throw new HttpError(400, 'Published requires a URL and verified publication date');
  if (event.status === 'scheduled' && !event.scheduledAt) throw new HttpError(400, 'Scheduled time required');
  if (input.notes != null) {
    if (typeof input.notes !== 'string' || input.notes.length > 4000) throw new HttpError(400, 'Invalid notes');
    event.notes = input.notes;
  }
  for (const name of ['views', 'likes', 'comments', 'shares', 'saves', 'reach']) {
    const value = input[name] ?? null;
    if (value !== null && (!Number.isSafeInteger(value) || value < 0)) throw new HttpError(400, 'Invalid ' + name);
    event[name] = value;
  }
  return event;
}
async function saveEvent(request, env, catalog) {
  const event = validateEvent(await readJson(request), catalog);
  for (let attempt = 0; attempt < 4; attempt++) {
    const current = await ledger(env, catalog.id);
    const existing = current.events.find(e => e.id === event.id);
    if (existing) return JSON.stringify(existing) === JSON.stringify(event) ? json(existing) : json({error: 'Event ID already has different content'}, 409);
    const previous = current.events.find(e => e.postId === event.postId);
    if (previous && (previous.videoId !== event.videoId || previous.platform !== event.platform)) throw new HttpError(409, 'Post identity cannot change');
    const result = await env.MEDIA.put(`viewer/observations/${catalog.id}.json`, JSON.stringify({events: [...current.events, event]}), {onlyIf: current.etag ? {etagMatches: current.etag} : {etagDoesNotMatch: '*'}, httpMetadata: {contentType: 'application/json', cacheControl: 'private, no-store'}});
    if (result) return json(event, 201);
  }
  throw new HttpError(409, 'Concurrent edit. Retry the same event ID');
}
function byteRange(value, size) {
  if (!value) return {offset: 0, length: size};
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) throw new HttpError(416, 'Invalid byte range');
  const start = match[1] ? Number(match[1]) : Math.max(0, size - Number(match[2]));
  const end = match[1] && match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || end < start) throw new HttpError(416, 'Invalid byte range');
  return {offset: start, length: end - start + 1};
}
async function* packedBytes(bucket, parts, selected) {
  let position = 0;
  for (const part of parts) {
    const start = Math.max(selected.offset, position);
    const end = Math.min(selected.offset + selected.length, position + part.length);
    if (start < end) {
      const object = await bucket.get(part.key);
      if (!object?.body) throw new Error('Archived chunk is missing');
      const stream = part.encoding === 'gzip' ? object.body.pipeThrough(new DecompressionStream('gzip')) : object.body;
      const reader = stream.getReader(); let skip = part.offset + start - position; let remaining = end - start;
      try {
        while (remaining) {
          const {value, done} = await reader.read();
          if (done) throw new Error('Archived chunk is truncated');
          if (skip >= value.length) { skip -= value.length; continue; }
          const bytes = value.subarray(skip, Math.min(value.length, skip + remaining));
          remaining -= bytes.length; skip = 0;
          yield bytes;
        }
      } finally { await reader.cancel(); }
    }
    position += part.length;
  }
}
async function media(request, env, item) {
  const partial = request.headers.has('Range');
  let selected;
  try { selected = byteRange(request.headers.get('Range'), item.bytes); }
  catch { return json({error: 'Range not satisfiable'}, 416, {'Content-Range': `bytes */${item.bytes}`}); }
  const location = item.location;
  const bucket = location && ['MEDIA', 'ARCHIVE'].includes(location.binding) && env[location.binding];
  if (!bucket) throw new HttpError(404, 'File is unavailable in this viewer');
  const safeName = (item.name || item.sha256).split('/').pop().replace(/[\r\n"\\]/g, '_');
  const download = new URL(request.url).searchParams.has('download') || !/^(image\/(png|jpeg|webp|gif)|video\/(mp4|webm|quicktime)|audio\/|text\/plain|application\/pdf)/.test(item.mime);
  const headers = {...security, 'Content-Security-Policy': "sandbox; default-src 'none'", 'Content-Type': download ? 'application/octet-stream' : item.mime, 'Content-Length': String(selected.length), 'Accept-Ranges': 'bytes', 'ETag': `"${item.sha256}"`, 'Content-Disposition': `${download ? 'attachment' : 'inline'}; filename="${safeName}"`};
  if (partial) headers['Content-Range'] = `bytes ${selected.offset}-${selected.offset + selected.length - 1}/${item.bytes}`;
  if (request.method === 'HEAD') return new Response(null, {status: partial ? 206 : 200, headers});
  let body;
  if (location.parts) {
    const iterator = packedBytes(bucket, location.parts, selected);
    body = new ReadableStream({async pull(controller) {
      try { const result = await iterator.next(); if (result.done) controller.close(); else controller.enqueue(result.value); }
      catch (error) { controller.error(error); }
    }, async cancel() { await iterator.return(); }});
  } else {
    const object = await bucket.get(location.key, {range: selected});
    if (!object?.body) throw new HttpError(404, 'Archived file is missing');
    body = object.body;
  }
  return new Response(body, {status: partial ? 206 : 200, headers});
}
async function archiveObject(request, env, path) {
  const match = /^\/api\/archive\/([a-z][a-z0-9-]{0,63})\/snapshots\/([a-f0-9]{64})\/(COMMITTED\.json|manifest\.json|transport\.json|payload\/[0-9]{6})$/.exec(path);
  if (!match) throw new HttpError(404, 'Snapshot object not found');
  const key = `apps/${match[1]}/snapshots/${match[2]}/${match[3]}`;
  if (request.method === 'GET') {
    const object = await env.MEDIA.get(key);
    if (!object?.body) throw new HttpError(404, 'Archived object is absent');
    return new Response(object.body, {headers: {...security, 'Content-Type': 'application/octet-stream'}});
  }
  if (request.method !== 'PUT') throw new HttpError(405, 'Method not allowed');
  const sha256 = request.headers.get('X-Outpost-SHA256');
  const length = Number(request.headers.get('Content-Length'));
  if (!/^[a-f0-9]{64}$/.test(sha256 || '') || !request.headers.has('Content-Length') || !Number.isSafeInteger(length) || length < 0) throw new HttpError(400, 'Checksum and byte count required');
  if (length > 80 * 1024 * 1024) throw new HttpError(413, 'Snapshot object exceeds the bounded size');
  const checksum = Uint8Array.from(sha256.match(/../g), value => Number.parseInt(value, 16)).buffer;
  const saved = await env.MEDIA.put(key, request.body || new Uint8Array(), {sha256: checksum, onlyIf: {etagDoesNotMatch: '*'}, httpMetadata: {contentType: 'application/octet-stream', cacheControl: 'private, no-store'}});
  if (!saved) throw new HttpError(409, 'Immutable archive object already exists');
  return json({stored: true, sha256, bytes: length}, 201);
}
async function serve(request, env) {
  const url = new URL(request.url), path = url.pathname;
  if (!path.startsWith('/api/')) {
    const response = await env.ASSETS.fetch(request);
    return new Response(response.body, {status: response.status, headers: {...Object.fromEntries(response.headers), ...security}});
  }
  if (request.headers.has('Origin') && request.headers.get('Origin') !== url.origin) throw new HttpError(403, 'Origin not allowed');
  if (!env.READ_KEY || !env.SESSION_SECRET || !env.ADMIN_TOKEN) throw new HttpError(503, 'Authentication is not configured');
  if (path.startsWith('/api/catalog/')) return handleCatalog(request, env, {admin, json});
  if (path.startsWith('/api/sources/')) return handleSources(request, env, {admin, authenticated, json, media});
  if (path === '/api/session' && request.method === 'POST') {
    const data = await readJson(request);
    if (!await sameSecret(data.key, env.READ_KEY) && !await validTicket(env, data.ticket || '', 'launch')) throw new HttpError(401, 'Access key is invalid');
    return json({ok: true}, 200, {'Set-Cookie': `outpost_session=${await ticket(env, 'session', 604800)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=604800`});
  }
  if (path === '/api/logout' && request.method === 'POST') return json({ok: true}, 200, {'Set-Cookie': 'outpost_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0'});
  const isAdmin = await admin(request, env);
  if (path.startsWith('/api/archive/')) {
    if (!isAdmin) throw new HttpError(401, 'Archive admin authorization required');
    return archiveObject(request, env, path);
  }
  if (path === '/api/launch' && request.method === 'POST') {
    if (!isAdmin) throw new HttpError(401, 'Update authorization required');
    return json({ticket: await ticket(env, 'launch', 90)});
  }
  if (!isAdmin && !await authenticated(request, env)) throw new HttpError(401, 'Unlock Outpost to continue');
  if (path === '/api/projects' && request.method === 'GET') {
    const index = await env.MEDIA.get('viewer/index.json');
    return json(index ? await index.json() : {projects: []});
  }
  const match = /^\/api\/projects\/([a-z][a-z0-9-]{0,63})(?:\/(events|assets\/([a-f0-9]{64})))?$/.exec(path);
  if (!match) throw new HttpError(404, 'Not found');
  const catalog = await project(env, match[1]);
  if (match[2] === 'events' && request.method === 'POST') {
    if (!isAdmin) throw new HttpError(401, 'Update authorization required');
    return saveEvent(request, env, catalog);
  }
  if (!['GET', 'HEAD'].includes(request.method)) throw new HttpError(405, 'Method not allowed');
  if (match[3]) {
    const item = catalog.assets.find(a => a.sha256 === match[3]);
    if (!item) throw new HttpError(404, 'File not found');
    return media(request, env, item);
  }
  const sourceVideoList = await sourceVideos(env, catalog.id);
  const known = new Set(catalog.videos.map(video => video.id));
  const videos = [...catalog.videos, ...sourceVideoList.filter(video => !known.has(video.video_id)).map(video => ({id:video.video_id,title:video.title||video.video_id,files:[],revisions:video.revisions}))];
  return json({...catalog, videos, source_catalog: sourceVideoList, observations: (await ledger(env, catalog.id)).events});
}
export default {
  async fetch(request, env) {
    try { return await serve(request, env); }
    catch (error) {
      if (!(error instanceof HttpError) && !(error instanceof CatalogError)) console.error(JSON.stringify({event: 'request_failed', path: new URL(request.url).pathname}));
      return json({error: error instanceof HttpError || error instanceof CatalogError ? error.message : 'Outpost could not load this request'}, error.status || 503);
    }
  }
};
