import test from 'node:test';
import assert from 'node:assert/strict';
import {readSSE} from '../../web/api.js';
import * as chat from '../../web/chat.js';

test('stream failure still consumes authoritative metadata and preserves saved message identity',async()=>{
  const answer={content:'',status:'running',sources:[]};
  async function* frames(){yield 'data: {"choices":[{"delta":{"content":"部分回答"}}]}\n\ndata: {"error":{"code":"upstream_disconnected"}}\n\ndata: {"metadata":{"conversation_id":"conversation-1","message_id":"answer-1","status":"error","content":"部分回答","sources":[],"error":"upstream_disconnected","latency_ms":42}}\n\ndata: [DONE]\n\n';}
  let final=false,conversationId;
  for await(const event of readSSE(frames())){const update=chat.applyChatEvent(answer,event);final=update.final||final;conversationId=update.conversationId||conversationId;}
  assert.equal(final,true);
  assert.equal(conversationId,'conversation-1');
  assert.equal(answer.id,'answer-1');
  assert.equal(answer.status,'error');
  assert.equal(answer.content,'部分回答');
  assert.equal(answer.latency_ms,42);
  assert.match(chat.chatErrorMessage(answer.error),/中断/);
});

function chatHost(){
  const nodes=new Map();
  const host={innerHTML:'',querySelector(selector){
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',value:'',checked:false,disabled:false,hidden:false,querySelectorAll:()=>[]});
    return nodes.get(selector);
  },querySelectorAll:selector=>selector==='[name=chatKB]:checked'?[{value:'kb-1'}]:[]};
  globalThis.document={querySelector:()=>null};
  host.querySelector('#chatTopK').value='6';host.querySelector('#chatAlpha').value='0.5';host.querySelector('#chatThreshold').value='0';host.querySelector('#chatTemperature').value='0.2';host.querySelector('#chatMaxTokens').value='1024';
  return {host,nodes};
}

async function submitChat({terminal=false,abort=false,savedStatus=null,refreshStatus=null}={}){
  const {host,nodes}=chatHost();const requests=[];let detailReads=0;
  const api={request:async path=>{
    requests.push(path);
    if(path==='/api/v1/knowledge-bases')return [{id:'kb-1',name:'KB'}];
    if(path==='/api/v1/conversations')return [];
    if(path==='/api/v1/model-profiles')return [];
    if(path==='/api/v1/conversations/conversation-1'){const status=++detailReads>1?refreshStatus:savedStatus;return {messages:status?[{id:'answer-1',role:'assistant',status,content:'服务器保存的回答',sources:[]}]:[]};}
    throw Error(path);
  },stream:async function*(){
    yield {conversation_id:'conversation-1',sources:[]};
    yield {choices:[{delta:{content:'部分回答'}}]};
    if(terminal)yield {metadata:{conversation_id:'conversation-1',message_id:'answer-1',status:'completed',content:'回答 [1]',sources:[],latency_ms:42}};
    if(abort)nodes.get('#stopChat').onclick();
    throw Error('synthetic read failure');
  }};
  const cleanup=await chat.render(host,{api,state:{},isCurrent:()=>true});
  const original=globalThis.FormData;
  globalThis.FormData=class{get(){return 'Policy?';}};
  try{await nodes.get('#chatForm').onsubmit({preventDefault(){},target:{elements:{query:{value:'Policy?'}}}});if(refreshStatus)await nodes.get('#refreshConversations').onclick();}
  finally{globalThis.FormData=original;cleanup();}
  return {html:nodes.get('#chatMessages').innerHTML,requests};
}

for(const abort of [false,true])test(`render preserves completed metadata through trailing ${abort?'abort':'read failure'}`,async()=>{
  const result=await submitChat({terminal:true,abort});
  assert.match(result.html,/回答完成/);
  assert.match(result.html,/回答 \[1\]/);
  assert.match(result.html,/42 ms/);
  assert.doesNotMatch(result.html,/生成失败|已取消/);
});

for(const abort of [false,true])test(`render reconciles pre-terminal ${abort?'abort':'disconnect'} without claiming cancellation`,async()=>{
  const result=await submitChat({abort});
  assert.ok(result.requests.includes('/api/v1/conversations/conversation-1'));
  assert.match(result.html,/状态待确认/);
  assert.doesNotMatch(result.html,/已取消|生成失败|回答完成/);
});

test('render reconciles disconnect with a server-saved completed answer',async()=>{
  const result=await submitChat({savedStatus:'completed'});
  assert.match(result.html,/回答完成/);
  assert.match(result.html,/服务器保存的回答/);
});

test('render keeps an unconfirmed status while saved server answer is still running',async()=>{
  const result=await submitChat({abort:true,savedStatus:'running'});
  assert.match(result.html,/状态待确认/);
  assert.doesNotMatch(result.html,/已取消|生成失败|回答完成/);
});

test('render confirms cancellation only from saved server state',async()=>{
  const result=await submitChat({abort:true,savedStatus:'canceled'});
  assert.match(result.html,/已取消/);
  assert.doesNotMatch(result.html,/状态待确认/);
});

test('refresh reloads the current conversation after an unconfirmed disconnect',async()=>{
  const result=await submitChat({refreshStatus:'completed'});
  assert.equal(result.requests.filter(path=>path==='/api/v1/conversations/conversation-1').length,2);
  assert.match(result.html,/回答完成/);
  assert.match(result.html,/服务器保存的回答/);
});

test('a delayed conversation refresh cannot replace a newer streaming answer',async()=>{
  const {host,nodes}=chatHost();
  let resolveRefresh,releaseStream,streamReady,streamCount=0;
  const refreshSnapshot=new Promise(resolve=>{resolveRefresh=resolve;});
  const streamGate=new Promise(resolve=>{releaseStream=resolve;});
  const ready=new Promise(resolve=>{streamReady=resolve;});
  const api={request:async path=>{
    if(path==='/api/v1/knowledge-bases')return [{id:'kb-1',name:'KB'}];
    if(path==='/api/v1/conversations'||path==='/api/v1/model-profiles')return [];
    if(path==='/api/v1/conversations/conversation-1')return refreshSnapshot;
    throw Error(path);
  },stream:async function*(){
    const second=++streamCount===2;
    yield {conversation_id:'conversation-1',sources:[]};
    if(second){yield {choices:[{delta:{content:'新的流式回答'}}]};streamReady();await streamGate;}
    yield {metadata:{conversation_id:'conversation-1',message_id:second?'answer-2':'answer-1',
      status:'completed',content:second?'新的确认回答':'旧回答',sources:[]}};
  }};
  const cleanup=await chat.render(host,{api,state:{},isCurrent:()=>true});
  const original=globalThis.FormData;
  globalThis.FormData=class{get(){return 'Policy?';}};
  const event=()=>({preventDefault(){},target:{elements:{query:{value:'Policy?'}}}});
  let pending;
  try{
    await nodes.get('#chatForm').onsubmit(event());
    const refresh=nodes.get('#refreshConversations').onclick();
    pending=nodes.get('#chatForm').onsubmit(event());
    await ready;
    assert.match(nodes.get('#chatMessages').innerHTML,/新的流式回答/);
    resolveRefresh({messages:[{id:'answer-1',role:'assistant',status:'completed',content:'旧回答',sources:[]}]});
    await refresh;
    assert.match(nodes.get('#chatMessages').innerHTML,/新的流式回答/);
    releaseStream();await pending;
    assert.match(nodes.get('#chatMessages').innerHTML,/新的确认回答/);
    assert.match(nodes.get('#chatMessages').innerHTML,/回答完成/);
  }finally{releaseStream();if(pending)await pending;globalThis.FormData=original;cleanup();}
});
