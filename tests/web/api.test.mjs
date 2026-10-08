import test from 'node:test';
import assert from 'node:assert/strict';

test('SSE handles split UTF-8 and CRLF frames without dropping the last frame', async () => {
  let api;
  try { api = await import('../../web/api.js'); } catch { assert.fail('Browser API module is missing'); }
  const encoder = new TextEncoder();
  const bytes = encoder.encode('data: {"choices":[{"delta":{"content":"退款"}}]}\r\n\r\ndata: {"status":"completed"}\n\ndata: [DONE]\n\n');
  async function* fragments() { for(let offset=0;offset<bytes.length;offset+=3) yield bytes.slice(offset,offset+3); }
  const received=[];
  for await (const event of api.readSSE(fragments())) received.push(event);
  assert.equal(received[0].choices[0].delta.content,'退款');
  assert.equal(received[1].status,'completed');
  assert.equal(received.length,2);
});

test('scope and CSRF headers are derived from current in-memory context', async () => {
  const {Client}=await import('../../web/api.js');
  const calls=[];
  const client=new Client(async(url,options)=>{calls.push({url,...options});return new Response('{"ok":true}',{headers:{'Content-Type':'application/json'}});});
  client.setContext('tenant-a','csrf-a');
  await client.request('/api/v1/kb',{method:'POST',json:{name:'demo'}});
  client.setContext('tenant-b','csrf-b');
  await client.request('/api/v1/kb');
  assert.equal(calls[0].headers.get('X-Workspace-ID'),'tenant-a');
  assert.equal(calls[0].headers.get('X-CSRF-Token'),'csrf-a');
  assert.equal(calls[1].headers.get('X-Workspace-ID'),'tenant-b');
  assert.equal(calls[1].headers.has('X-CSRF-Token'),false);
  assert.equal(calls[0].credentials,'same-origin');
});

test('errors preserve status and readable server detail; no-content is supported', async () => {
  const {Client}=await import('../../web/api.js');
  const denied=new Client(async()=>new Response('{"detail":"Quota exceeded"}',{status:429}));
  await assert.rejects(denied.request('/api/v1/kb'),error=>error.status===429&&error.message==='Quota exceeded');
  const empty=new Client(async()=>new Response(null,{status:204}));
  assert.equal(await empty.request('/api/v1/kb',{method:'DELETE'}),null);
});
test('SSE accepts a final JSON frame without trailing delimiter and rejects corrupted JSON', async()=>{
  const {readSSE}=await import('../../web/api.js');async function* fragments(){yield ': keepalive\n\nevent: complete\ndata: {"metadata":{"status":"completed","content":"完毕"}}';}
  const values=[];for await(const event of readSSE(fragments()))values.push(event);assert.equal(values[0].metadata.content,'完毕');
  async function* broken(){yield 'data: {bad}\n\n';}await assert.rejects(async()=>{for await(const event of readSSE(broken())){}},/无法解析/);
});
