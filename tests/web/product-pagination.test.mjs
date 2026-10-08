import test from 'node:test';
import assert from 'node:assert/strict';
import {render as renderKnowledge} from '../../web/knowledge.js';
import {render as renderEvaluation} from '../../web/evaluation.js';

class Element {
  constructor() { this.html=''; this.onclick=null; this.onchange=null; this.value=''; this.disabled=false; this.options=[]; this.classList={toggle(){},add(){},remove(){}}; }
  set innerHTML(value) { this.html=value; this.options=[...value.matchAll(/<option value="([^"]+)"([^>]*)>/g)].map(([,id,attributes])=>({value:id,selected:attributes.includes('selected')})); }
  get innerHTML() { return this.html; }
  get selectedOptions() { return (this.options||[]).filter(option=>option.selected); }
  querySelector(selector) { return this.children?.get(selector)||null; }
  querySelectorAll() { return []; }
  addEventListener() {}
}

function hostWithIds() {
  const host=new Element();
  const nodes=new Map();
  host.querySelector=selector=>{
    if(selector.startsWith('#')) { if(!nodes.has(selector))nodes.set(selector,new Element()); return nodes.get(selector); }
    if(selector.startsWith('[data-kb='))return new Element();
    if(selector.startsWith('[data-active='))return {checked:true};
    return null;
  };
  host.querySelectorAll=selector=>selector.startsWith('[name=evaluationKB]')?[{value:'kb-1',checked:true,onchange:null}]:[];
  const panel=host.querySelector('#knowledgeDetail');
  panel.querySelector=host.querySelector;
  panel.querySelectorAll=()=>[];
  return {host,nodes};
}

test('knowledge management reaches document 101 and browses all returned import jobs',async()=>{
  globalThis.setInterval=()=>1;
  globalThis.clearInterval=()=>{};
  const {host,nodes}=hostWithIds();
  const paths=[];
  const jobs=Array.from({length:35},(_,index)=>({id:`job-${index+1}`,filename:`job-${index+1}.txt`,status:'completed'}));
  const api={request:async path=>{
    paths.push(path);
    if(path==='/api/v1/knowledge-bases')return {items:[{id:'kb-1',name:'KB',role:'owner'}]};
    if(path==='/api/v1/knowledge-bases/kb-1')return {id:'kb-1',name:'KB',doc_count:101};
    if(path.startsWith('/api/v1/kb/kb-1/documents?'))return {items:[{id:path.includes('page=2')?'doc-101':'doc-1',filename:path.includes('page=2')?'late.txt':'first.txt',status:'completed'}],total:101,page:path.includes('page=2')?2:1,page_size:100};
    if(path==='/api/v1/ingestion-jobs')return {jobs};
    throw Error(path);
  }};
  const cleanup=await renderKnowledge(host,{api,state:{workspace:{role:'owner'}},route(){},isCurrent:()=>true});
  assert.match(nodes.get('#knowledgeDetail').innerHTML,/first\.txt/);
  assert.ok(nodes.get('#nextDocuments')?.onclick,'document page control exists');
  await nodes.get('#nextDocuments').onclick();
  assert.match(nodes.get('#knowledgeDetail').innerHTML,/late\.txt/);
  assert.ok(paths.includes('/api/v1/kb/kb-1/documents?page=2&page_size=100'));
  assert.ok(nodes.get('#nextJobs')?.onclick,'job page control exists');
  await nodes.get('#nextJobs').onclick();
  assert.match(nodes.get('#jobList').innerHTML,/job-31\.txt/);
  cleanup();
});

test('evaluation retains labels chosen on separate document pages in submitted request',async()=>{
  const {host,nodes}=hostWithIds();
  let submitted=null;
  const api={request:async(path,options)=>{
    if(path==='/api/v1/knowledge-bases')return {items:[{id:'kb-1',name:'KB'}]};
    if(path.startsWith('/api/v1/kb/kb-1/documents?'))return {items:[{id:path.includes('page=2')?'doc-101':'doc-1',filename:path.includes('page=2')?'late.txt':'first.txt',status:'completed'}],total:101,page:path.includes('page=2')?2:1,page_size:100};
    if(path==='/api/v1/evaluation/retrieval'){submitted=options.json;return {comparisons:[]};}
    throw Error(path);
  }};
  await renderEvaluation(host,{api,state:{},isCurrent:()=>true});
  const select=nodes.get('#relevantDocs');
  select.options[0].selected=true;
  select.onchange();
  assert.ok(nodes.get('#nextTruthDocs')?.onclick,'label page control exists');
  await nodes.get('#nextTruthDocs').onclick();
  select.options[0].selected=true;
  select.onchange();
  await nodes.get('#prevTruthDocs').onclick();
  assert.equal(select.options[0].selected,true,'first-page label remains selected after returning');
  host.querySelector('#evaluationQuery').value='How?';
  await nodes.get('#runEvaluation').onclick();
  assert.deepEqual(submitted.queries[0].relevant_doc_ids,['doc-1','doc-101']);
});

test('evaluation renders hit rate and nDCG and exports the API metric values',async()=>{
  const {host,nodes}=hostWithIds();let exported;
  const metrics={context_precision:0.5,context_recall:0.667,mrr:0.5,hit_rate:1,ndcg:0.5307212739772434};
  const api={request:async path=>{
    if(path==='/api/v1/knowledge-bases')return [{id:'kb-1',name:'KB'}];
    if(path.startsWith('/api/v1/kb/kb-1/documents?'))return {items:[],total:0,page:1,page_size:100};
    if(path==='/api/v1/evaluation/retrieval')return {comparisons:[{config:{name:'test'},query:'How?',metrics,results:[]}]};
    throw Error(path);
  }};
  globalThis.document={querySelector:()=>null,createElement:()=>({setAttribute(){},append(){},click(){}})};
  const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL;
  URL.createObjectURL=blob=>{exported=blob;return 'blob:synthetic';};URL.revokeObjectURL=()=>{};
  try{
    await renderEvaluation(host,{api,state:{},isCurrent:()=>true});
    host.querySelector('#evaluationQuery').value='How?';
    await nodes.get('#runEvaluation').onclick();
    const html=nodes.get('#evaluationResults').innerHTML;
    assert.match(html,/Hit Rate/);assert.match(html,/nDCG/);assert.match(html,/<td>1\.000<\/td><td>0\.531<\/td>/);
    nodes.get('#exportEvaluation').onclick();
    assert.deepEqual(JSON.parse(await exported.text()).comparisons[0].metrics,metrics);
  }finally{URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;}
});
