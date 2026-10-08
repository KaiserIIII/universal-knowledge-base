import test from 'node:test';
import assert from 'node:assert/strict';

class Element {
  constructor(){this.html='';this.children=[];this.value='';this._textContent='';this.dataset={};this.classList={add(){},remove(){},toggle(){}};this.nodes=new Map();}
  set innerHTML(value){this.html=value;this.children=[];}
  get innerHTML(){return this.html;}
  set textContent(value){this._textContent=String(value);this.html='';}
  get textContent(){return this._textContent||this.html.replace(/<[^>]+>/g,'');}
  replaceChildren(...children){this.children=children;this.html=children.map(child=>child.innerHTML).join('');}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  querySelectorAll(){return [];}
  setAttribute(){} removeAttribute(){} addEventListener(){}
}

test('English actual navigation, knowledge form and help preserve Chinese API content',async()=>{
  const elements=new Map();
  globalThis.document={title:'',documentElement:{lang:''},querySelector(selector){if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);},querySelectorAll(){return [];},createElement(){return new Element();},addEventListener(){}};
  globalThis.window={addEventListener(){},innerWidth:1200,confirm(){return true;}};
  globalThis.location={href:'https://synthetic.example/?lang=en#knowledge',search:'?lang=en',hash:'#knowledge'};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.setInterval=()=>1;globalThis.clearInterval=()=>{};
  const data={
    '/api/v1/auth/me':{user:{name:'中文合成成员',email:'synthetic@example.com'},organizations:[{id:'org',name:'中文合成组织',role:'owner'}],csrf_token:'synthetic'},
    '/api/v1/knowledge-bases':{items:[{id:'kb',name:'中文合成知识库',role:'owner'}]},
    '/api/v1/knowledge-bases/kb':{id:'kb',name:'中文合成知识库',description:'知识库：中文合成资料',doc_count:1},
    '/api/v1/kb/kb/documents?page=1&page_size=100':{items:[{id:'doc',filename:'中文合成文档.txt',status:'completed'}],total:1,page:1,page_size:100},
    '/api/v1/ingestion-jobs':{jobs:[]}
  };
  globalThis.fetch=async path=>new Response(JSON.stringify(data[path]||{}),{status:200});
  await import('../../web/app.js');
  assert.match(elements.get('#navigation').innerHTML,/Visual workflows/);
  for(let attempt=0;attempt<50;attempt++){
    const detail=elements.get('#view').children[0]?.querySelector('#knowledgeDetail');
    if(detail?.innerHTML.includes('Import documents'))break;
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  const rendered=elements.get('#view').children[0];
  assert.match(rendered.innerHTML,/Knowledge bases/);
  assert.ok(rendered.innerHTML.includes('中文合成知识库'));
  const detail=rendered.querySelector('#knowledgeDetail').innerHTML;
  assert.match(detail,/Import documents/);
  assert.match(detail,/Embedding/);
  assert.ok(detail.includes('知识库：中文合成资料'));
  assert.ok(detail.includes('中文合成文档.txt'));
  assert.equal(elements.get('#accountName').textContent,'中文合成成员');
  assert.ok(elements.get('#workspace').innerHTML.includes('中文合成组织'));
});

test('static fragments translate while interpolated strings stay unchanged',async()=>{
  const {t,html,setLocale}=await import('../../web/i18n.js');
  setLocale('en');assert.equal(t('知识库'),'Knowledge bases');
  const value='知识库：中文合成资料';
  assert.equal(html`<h2>知识库</h2><p>${value}</p>`,'<h2>Knowledge bases</h2><p>知识库：中文合成资料</p>');
  assert.equal(t('Unknown server detail 中文合成错误'),'Unknown server detail 中文合成错误');
  setLocale('zh-CN');assert.equal(t('知识库'),'知识库');
});

test('locale validates URL and preference and handles unavailable storage',async()=>{
  const {initializeLocale,getLocale,setLocale}=await import('../../web/i18n.js');
  const unavailable={getItem(){throw Error('unavailable');},setItem(){throw Error('unavailable');}};
  initializeLocale({url:'https://synthetic.example/?lang=en#chat',storage:unavailable});assert.equal(getLocale(),'en');
  initializeLocale({url:'https://synthetic.example/?lang=invalid',storage:{getItem:()=> 'en'}});assert.equal(getLocale(),'en');
  initializeLocale({url:'https://synthetic.example/',storage:{getItem:()=> '<script>'}});assert.equal(getLocale(),'zh-CN');
  assert.equal(setLocale('invalid'),false);assert.equal(getLocale(),'zh-CN');
});

test('cancel does not write or navigate; confirmed reload preserves route and fallback URL',async()=>{
  const {switchLocale,setLocale,localeURL}=await import('../../web/i18n.js');setLocale('zh-CN');
  let writes=0,assigned=null,warning='';
  const storage={setItem(){writes++;throw Error('unavailable');}};
  const location={href:'https://synthetic.example/?other=1#workflows?run=synthetic',assign:url=>{assigned=url;}};
  assert.equal(switchLocale('en',{storage,location,confirm:message=>{warning=message;return false;}}),false);
  assert.equal(writes,0);assert.equal(assigned,null);assert.match(warning,/未保存/);assert.match(warning,/进行中/);
  assert.equal(switchLocale('en',{storage,location,confirm:()=>true}),true);assert.equal(writes,1);
  const result=new URL(assigned);assert.equal(result.searchParams.get('lang'),'en');assert.equal(result.searchParams.get('other'),'1');assert.equal(result.hash,'#workflows?run=synthetic');
  setLocale('en');assert.equal(new URL(localeURL('/docs',location.href)).searchParams.get('lang'),'en');
});
