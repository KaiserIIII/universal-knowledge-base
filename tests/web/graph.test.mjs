import test from 'node:test';
import assert from 'node:assert/strict';
import {GraphHistory,connect,removeNode,validateGraph,template,makeNode} from '../../web/graph.js';

test('connections reject cycles, duplicates and invalid typed ports without mutation',()=>{
  const graph=template('strict');
  const original=JSON.stringify(graph);
  assert.throws(()=>connect(graph,'output','input'),/端口|循环/);
  assert.throws(()=>connect(graph,'input','retrieve'),/重复/);
  assert.throws(()=>connect(graph,'input','model'),/端口/);
  assert.equal(JSON.stringify(graph),original);
  assert.deepEqual(validateGraph(graph),[]);
});
test('remove node removes its incident edges and undo/redo restores snapshots',()=>{
  const graph=template('strict');const history=new GraphHistory(graph);
  const next=removeNode(graph,'retrieve');history.push(next);
  assert.ok(next.edges.every(edge=>edge.source!=='retrieve'&&edge.target!=='retrieve'));
  assert.equal(history.undo().nodes.length,graph.nodes.length);
  assert.equal(history.redo().nodes.length,graph.nodes.length-1);
  const snapshot=history.current();snapshot.nodes[0].label='mutated';
  assert.notEqual(history.current().nodes[0].label,'mutated');
});
test('templates have valid acyclic paths and dual templates expose independent branches',()=>{
  for(const name of ['strict','multi-kb','multi-model']) assert.deepEqual(validateGraph(template(name)),[]);
  assert.equal(template('multi-model').nodes.filter(node=>node.type==='model').length,2);
  assert.equal(template('multi-kb').nodes.filter(node=>node.type==='retrieval').length,2);
});

test('file evidence connects to grounding and model modules',()=>{
  const graph=template();
  graph.nodes[1]=makeNode('files','retrieve',310,180);
  assert.deepEqual(validateGraph(graph),[]);
  assert.deepEqual(graph.nodes[1].config.doc_ids,[]);
});
test('import validation rejects nonnumeric coordinates and malformed configuration',()=>{
  assert.ok(validateGraph({nodes:[null],edges:[]}).length);
  assert.ok(validateGraph({nodes:[],edges:[null]}).length);
  const ports=template();ports.edges[0].source_port='missing';assert.ok(validateGraph(ports).includes('端口名称错误'));
  const graph=template();graph.nodes[0].position.x='" onload="bad';graph.nodes[1].config=[];
  assert.ok(validateGraph(graph).some(error=>error.includes('坐标')));
  assert.ok(validateGraph(graph).some(error=>error.includes('参数')));
});
test('a reachable branch that cannot contribute to output is rejected',()=>{
  let graph=template();graph.nodes.push(makeNode('retrieval','unused',310,400));graph=connect(graph,'input','unused');
  assert.ok(validateGraph(graph).some(error=>error.includes('输出')));
});
