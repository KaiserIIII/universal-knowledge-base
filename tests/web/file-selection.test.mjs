import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeFileOptions} from '../../web/file-selection.js';

test('saved files beyond the first page remain available and selected',()=>{
  const result=mergeFileOptions([], [{id:'doc-1',filename:'First file'}], ['doc-201']);
  assert.equal(result.find(row=>row.id==='doc-201').selected,true);
  const next=mergeFileOptions(result, [{id:'doc-201',filename:'Later file'}], ['doc-201']);
  assert.equal(next.find(row=>row.id==='doc-201').filename,'Later file');
  assert.equal(next.filter(row=>row.id==='doc-201').length,1);
});

test('loading another page respects a deliberate deselection',()=>{
  const first=mergeFileOptions([], [{id:'doc-1',filename:'First'}], ['doc-1']);
  const next=mergeFileOptions(first, [{id:'doc-101',filename:'Next'}], []);
  assert.ok(next.every(row=>!row.selected));
});
