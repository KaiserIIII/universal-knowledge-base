import test from 'node:test';
import assert from 'node:assert/strict';

function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
class Element {
  constructor(){this.html='';this.children=[];this.classList={add(){},remove(){},toggle(){}};this.dataset={};}
  set innerHTML(value){this.html=value;this.children=[];}
  get innerHTML(){return this.html;}
  replaceChildren(...children){this.children=children;this.html='';}
  querySelector(){return new Element();}
  querySelectorAll(){return [];}
  setAttribute(){} removeAttribute(){}
}

test('an old render cannot mount while the new organization identity is pending',async()=>{
  globalThis.setInterval=()=>1;
  globalThis.clearInterval=()=>{};
  const elements=new Map();
  globalThis.document={title:'',querySelector(selector){if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);},querySelectorAll(){return [];},createElement(){return new Element();},addEventListener(){}};
  globalThis.window={addEventListener(){},innerWidth:1200};
  globalThis.location={hash:'#knowledge'};
  globalThis.fetch=async()=>new Response('{"detail":"Authentication required"}',{status:401,headers:{'Content-Type':'application/json'}});
  const app=await import('../../web/app.js');
  const {api}=await import('../../web/api.js');
  app.state.user={name:'A',email:'a@example.com'};
  app.state.workspace={id:'org-a',role:'owner'};
  app.state.csrf='csrf';
  const oldRender=deferred(),newIdentity=deferred();
  api.fetcher=async path=>{
    if(path==='/api/v1/knowledge-bases'){await oldRender.promise;return new Response('{"items":[]}');}
    if(path==='/api/v1/ingestion-jobs')return new Response('{"jobs":[]}');
    if(path==='/api/v1/auth/me'){await newIdentity.promise;return new Response('{"user":{"name":"B","email":"b@example.com"},"organizations":[{"id":"org-b","role":"owner"}],"csrf_token":"new"}');}
    throw Error(path);
  };
  const pendingRoute=app.route();
  await Promise.resolve();
  const switching=document.querySelector('#workspace').onchange({target:{value:'org-b'}});
  const navigationDuringSwitch=app.route();
  oldRender.resolve();
  await Promise.all([pendingRoute,navigationDuringSwitch]);
  assert.equal(document.querySelector('#view').children.length,0);
  assert.match(document.querySelector('#view').innerHTML,/正在加载/);
  newIdentity.resolve();
  await switching;
  assert.equal(app.state.workspace.id,'org-b');
  api.fetcher=async path=>{
    if(path==='/api/v1/auth/me')return new Response('{"user":{"name":"B","email":"b@example.com"},"organizations":[],"csrf_token":"new"}');
    if(path==='/api/v1/auth/logout')return new Response(null,{status:204});
    throw Error(path);
  };
  await app.switchOrganization('org-b');
  assert.equal(app.state.workspace,null);
  assert.match(document.querySelector('#view').innerHTML,/没有工作空间/);
  await document.querySelector('#logoutButton').onclick();
  assert.equal(app.state.user,null);
  assert.equal(document.querySelector('#view').children.length,0);
});
