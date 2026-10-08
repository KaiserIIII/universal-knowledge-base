import test from 'node:test';
import assert from 'node:assert/strict';
import * as runs from '../../web/workflow-run.js';

test('async workflow polls until the server confirms a terminal result',async()=>{
  const updates=[],calls=[];let polls=0;
  const api={request:async(path,options)=>{calls.push({path,options});if(options?.method==='POST')return {id:'run-1',status:'queued'};return ++polls===1?{id:'run-1',status:'running'}:{id:'run-1',status:'completed',answer:'已完成'};}};
  const result=await runs.runWorkflow(api,'workflow-1',{query:'合成问题',use_published:true},{pollInterval:0,onUpdate:run=>updates.push(run.status)});
  assert.deepEqual(updates,['queued','running','completed']);
  assert.equal(result.answer,'已完成');
  assert.equal(calls[1].path,'/api/v1/workflows/workflow-1/runs/run-1');
});

test('cancel requested before creation response still cancels the accepted server run',async()=>{
  const control=new AbortController(),calls=[];let accept;
  const created=new Promise(resolve=>{accept=resolve;});
  const api={request:async(path,options)=>{calls.push({path,options});if(path.endsWith('/run'))return created;if(path.endsWith('/cancel'))return {id:'run-2',status:'canceling'};return {id:'run-2',status:'canceled'};}};
  const running=runs.runWorkflow(api,'workflow-2',{query:'合成问题'},{signal:control.signal,pollInterval:0});
  control.abort();accept({id:'run-2',status:'queued'});
  const result=await running;
  assert.equal(result.status,'canceled');
  assert.equal(calls[0].options.signal,undefined);
  assert.equal(calls.filter(call=>call.path.endsWith('/cancel')).length,1);
  assert.equal(calls[1].path,'/api/v1/workflows/workflow-2/runs/run-2/cancel');
});

test('canceling is not presented as canceled before the terminal state is confirmed',async()=>{
  const control=new AbortController(),updates=[];let polls=0;
  const api={request:async(path,options)=>{if(path.endsWith('/run'))return {id:'run-3',status:'running'};if(path.endsWith('/cancel'))return {id:'run-3',status:'canceling'};return ++polls===1?{id:'run-3',status:'canceling'}:{id:'run-3',status:'canceled'};}};
  const result=await runs.runWorkflow(api,'workflow-3',{query:'合成问题'},{signal:control.signal,pollInterval:0,onUpdate:run=>{updates.push(run.status);if(run.status==='running')control.abort();}});
  assert.equal(result.status,'canceled');
  assert.ok(updates.indexOf('canceling')<updates.indexOf('canceled'));
});
