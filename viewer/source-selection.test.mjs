import test from 'node:test';
import assert from 'node:assert/strict';
import {exactSource} from './public/sources.mjs';
test('render selection chooses only its pinned revision; old outputs do not guess',()=>{
  const records={revisions:[{id:'old'},{id:'new'}]};
  assert.equal(exactSource(records,{source_revision_id:'old'}),'old');
  assert.equal(exactSource(records,{sha256:'shared-output'}),null);
  assert.equal(exactSource(records,{source_revision_id:'another-video'}),null);
});
