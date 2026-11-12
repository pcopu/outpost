import test from 'node:test';
import assert from 'node:assert/strict';
let selectRender;
try { ({selectRender} = await import('./public/renders.mjs')); } catch {}
test('render links select an exact revision within the chosen video', () => {
  assert.ok(selectRender, 'Render selection is implemented');
  const versions = [{sha256: 'first'}, {sha256: 'second'}, {sha256: 'rejected', reviewStatus: 'rejected'}];
  assert.equal(selectRender(versions, 'first'), versions[0]);
  assert.equal(selectRender(versions, 'rejected'), versions[2]);
  assert.equal(selectRender(versions, 'another-video'), versions[1]);
  assert.equal(selectRender(versions, null), versions[1]);
  assert.equal(selectRender([], null), null);
});
