// The portable Python restorer validates the same wire format independently.
import {CatalogError} from './catalog.mjs';
export const CHUNK = 8 * 1024 * 1024;
export const MANIFEST_LIMIT = 900000;
export const ensure = (condition, message, status=400) => {if(!condition)throw new CatalogError(status,message);};
export const bytes = value => new TextEncoder().encode(value);
export const stable = value => value && typeof value==='object' ? Array.isArray(value) ? value.map(stable) : Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])])) : value;
export const canonical = value => JSON.stringify(stable(value));
export const digest = async value => [...new Uint8Array(await crypto.subtle.digest('SHA-256',value))].map(n=>n.toString(16).padStart(2,'0')).join('');
export const hash = value => typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
export function safePath(value) {
  ensure(typeof value==='string' && value.length>0 && bytes(value).length<=800 && value.normalize('NFC')===value,'Invalid portable path');
  for(const part of value.split('/'))ensure(part && !['.','..'].includes(part) && !/[. ]$/.test(part) && !/[\\:<>"|?*\x00-\x1f\x7f]/.test(part) && !/^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\..*)?$/i.test(part) && !['.git','.env','outpost-source.json'].includes(part.toLowerCase()) && !/^(\.env\.|\.outpost-)/i.test(part),'Unsafe or reserved restore path');
  return value;
}
export function validateFile(file) {
  safePath(file.path);
  ensure(hash(file.sha256) && Number.isSafeInteger(file.bytes) && file.bytes>=0 && file.bytes<=20*1024**3,'Invalid source checksum/size');
  ensure(['source','transcript','audio','raw','font','dependency','archive','reference','render'].includes(file.role),'Invalid source role');
  ensure(file.language==null || typeof file.language==='string' && /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$/.test(file.language),'Invalid language');
  const locator=file.locator;
  if(locator?.kind==='chunks') {
    ensure(Array.isArray(locator.chunks) && locator.chunks.length<=2560,'Invalid chunk list');
    ensure(locator.chunks.every(c=>hash(c.sha256)&&Number.isSafeInteger(c.bytes)&&c.bytes>0&&c.bytes<=CHUNK) && locator.chunks.reduce((n,c)=>n+c.bytes,0)===file.bytes,'Source chunk size mismatch');
  } else {
    ensure(locator?.kind==='snapshot' && hash(locator.snapshot_id),'Invalid source locator');
    ensure(safePath(locator.source_path).startsWith('files/'),'Select an archived file');
  }
}
export async function validateRevision(revision,app) {
  ensure(revision?.format_version===1 && revision.manifest && hash(revision.revision_id),'Invalid source revision');
  ensure(bytes(canonical(revision)).length<=MANIFEST_LIMIT,'Source manifest exceeds bounded size',413);
  const m=revision.manifest;
  function numeric(value){
    if(typeof value==='number')ensure(Number.isSafeInteger(value),'Use strings for decimal or oversized numeric source metadata');
    if(value&&typeof value==='object'){
      if(!Array.isArray(value))ensure(Object.keys(value).every(key=>/^[\x00-\x7f]*$/.test(key)&&!/^\d+$/.test(key)),'Source metadata keys must be named ASCII keys');
      Object.values(value).forEach(numeric);
    }
  }
  numeric(m);
  ensure(m.app_id===app && typeof m.video_id==='string' && m.video_id.length>0 && m.video_id.length<=200 && !/[\x00-\x1f]/.test(m.video_id),'Source identity mismatch');
  ensure(typeof revision.label==='string' && revision.label.length<=300,'Invalid source label');
  ensure(await digest(bytes(canonical(m)))===revision.revision_id,'Source revision checksum mismatch');
  ensure(Array.isArray(m.files) && m.files.length>0 && m.files.length<=2000,'Invalid source file count');
  const names=new Set();let total=0;
  for(const file of m.files) {
    validateFile(file);const name=file.path.toUpperCase().toLowerCase();
    ensure(![...names].some(p=>p===name||p.startsWith(name+'/')||name.startsWith(p+'/')),'Case collision or file/directory conflict');
    names.add(name);total+=file.bytes;
  }
  ensure(total<=20*1024**3,'Source project exceeds restore limit');
  ensure(m.recipe && (m.recipe.entrypoint==null || m.files.some(f=>f.path===m.recipe.entrypoint)) && Array.isArray(m.recipe.command) && m.recipe.command.every(v=>typeof v==='string') && m.recipe.tools && typeof m.recipe.tools==='object','Invalid recipe');
  ensure(Array.isArray(m.dependencies) && Array.isArray(m.lineage) && m.git && ['clean','dirty','unknown'].includes(m.git.working_tree) && hash(m.git.source_tree_sha256),'Source provenance required');
  ensure(m.git.source_tree_sha256===await digest(bytes(canonical(m.files.map(f=>({path:f.path,sha256:f.sha256,bytes:f.bytes}))))),'Selected source tree checksum mismatch');
  for(const dep of m.dependencies)ensure(typeof dep.name==='string' && dep.name && ['present','missing','excluded','unknown','declared'].includes(dep.status) && (dep.status!=='present'||m.files.some(f=>f.path===dep.path&&f.sha256===dep.sha256)),'Invalid dependency evidence');
  for(const parent of m.lineage)ensure(parent.app_id===app && typeof parent.video_id==='string' && hash(parent.revision_id),'Lineage must pin a same-app source revision');
  return m;
}
