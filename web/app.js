import {t,html,staticHTML,localizePage,bindLanguageControl,localeURL,getLocale} from './i18n.js';
import {api,Client} from './api.js';
import {$,esc,toast,formJSON,showModal} from './dom.js';

const nav=[['overview','◈','概览'],['knowledge','▤','知识库'],['chat','◇','对话工作台'],['workflows','⌘','可视化编排'],['models','◉','模型连接'],['evaluation','◎','检索实验室'],['team','♧','团队与权限'],['billing','▣','订阅与用量'],['settings','⚙','工作空间设置']];
export const state={user:null,organizations:[],workspace:null,csrf:null,view:null,cleanup:null};
let routeRevision=0;
let identityPending=false;

export async function refreshIdentity(preferred=null,isCurrent=()=>true) {
  const data=await api.request('/api/v1/auth/me');if(!isCurrent())return null;state.user=data.user;state.organizations=data.organizations;state.csrf=data.csrf_token;
  state.workspace=state.organizations.find(item=>item.id===(preferred||state.workspace?.id))||state.organizations[0]||null;
  api.setContext(state.workspace?.id,state.csrf);
  $('#workspace').innerHTML=state.organizations.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
  $('#workspace').value=state.workspace?.id||'';$('#roleBadge').textContent=({owner:t('所有者'),admin:t('管理员'),editor:t('知识编辑'),viewer:t('只读成员')})[state.workspace?.role]||t('未加入组织');
  $('#accountName').textContent=state.user.name||state.user.email.split('@')[0];$('#accountEmail').textContent=state.user.email;$('#avatar').textContent=state.user.email[0].toUpperCase();
  return data;
}

function authScreen(register=false) {
  ++routeRevision;identityPending=false;$('#view').replaceChildren();
  state.cleanup?.();state.cleanup=null;api.setContext(null,null);state.user=null;state.workspace=null;
  $('#boot').classList.add('hidden');$('#shell').classList.add('hidden');$('#auth').classList.remove('hidden');
  $('#auth').innerHTML=html`<section class="auth-story"><div class="brand"><div class="brand-mark">知</div><span><strong>知序</strong><small>ENTERPRISE KNOWLEDGE</small></span></div><div class="eyebrow">YOUR KNOWLEDGE. CONNECTED.</div><h1>让团队知识<br>成为<span>可靠的答案。</span></h1><p>从资料入库，到检索编排、证据问答与反馈运营。把每一个答案，连接到团队真正掌握的知识。</p><div class="auth-features"><div class="auth-feature"><span>↗</span> 多知识库连接 · 多模型协同</div><div class="auth-feature"><span>◎</span> 回答来源可查 · 检索效果可测</div><div class="auth-feature"><span>⌘</span> 模块自由编排 · 组织权限隔离</div></div><small style="margin-top:65px">SELF-HOSTED · EVIDENCE-DRIVEN</small></section><section class="auth-form-area"><div class="auth-form"><label class="language-label">Language / 语言<select id="authLanguage" aria-label="Language / 语言"><option value="zh-CN">简体中文</option><option value="en">English</option></select></label><div class="eyebrow">KNOWLEDGE WORKSPACE</div><h2>${register?t('创建你的工作空间'):t('欢迎回来')}</h2><p>${register?t('建立组织，开始连接团队的知识与模型。'):t('登录，继续管理你的团队知识。')}</p><form id="authForm">${register?staticHTML('<label>工作空间名称<input name="organization_name" placeholder="例如：产品支持团队" maxlength="128" required autocomplete="organization"></label>'):''}<label>邮箱<input name="email" type="email" placeholder="you@company.com" required autocomplete="username"></label><label>密码<input name="password" type="password" placeholder="${register?t('至少 12 个字符'):t('请输入密码')}" minlength="${register?12:1}" maxlength="1024" required autocomplete="${register?'new-password':'current-password'}"></label><div id="authError" class="error-state hidden" role="alert"></div><button class="primary" type="submit">${register?t('创建工作空间'):t('登录工作台')} <span>→</span></button></form><div class="auth-switch">${register?t('已有账号？'):t('还没有工作空间？')} <button id="authToggle">${register?t('登录'):t('创建账号')}</button></div><div class="auth-note">资料保存在你部署的服务中。模型连接与数据发送范围由组织管理员配置。</div></div></section>`;
  bindLanguageControl($('#authLanguage'));
  $('#authToggle').onclick=()=>authScreen(!register);
  $('#authForm').onsubmit=async event=>{
    event.preventDefault();const button=$('[type=submit]',event.target);button.disabled=true;$('#authError').classList.add('hidden');
    try{await api.request(`/api/v1/auth/${register?'register':'login'}`,{method:'POST',json:formJSON(event.target)});await refreshIdentity();showShell();await route();}
    catch(error){$('#authError').textContent=error.message;$('#authError').classList.remove('hidden');}finally{button.disabled=false;}
  };
}

function showShell() {
  $('#boot').classList.add('hidden');$('#auth').classList.add('hidden');$('#shell').classList.remove('hidden');
  $('#navigation').innerHTML=nav.map(([id,icon,title])=>`<a href="#${id}" data-nav="${id}"><span class="nav-icon">${icon}</span>${t(title)}${id==='workflows'?'<span class="nav-count">FLOW</span>':''}</a>`).join('');
  localizePage();bindLanguageControl($('#languageControl'));
}

function invalidateView(){
  const revision=++routeRevision;
  identityPending=false;
  state.cleanup?.();state.cleanup=null;
  $('#view').innerHTML=staticHTML('<div class="loading">正在加载…</div>');
  return revision;
}

export async function switchOrganization(id){
  const revision=invalidateView();
  identityPending=true;
  try{
    await refreshIdentity(id,()=>revision===routeRevision);
    if(revision===routeRevision){identityPending=false;await route();return true;}
    return false;
  }catch(error){
    if(revision!==routeRevision)return false;
    identityPending=false;
    if(error.status===401){authScreen();return false;}
    $('#view').innerHTML=html`<div class="error-state"><strong>无法切换工作空间</strong><p>${esc(error.message)}</p><button id="retryView" class="secondary">重新连接</button></div>`;
    $('#retryView').onclick=()=>switchOrganization(id);
    return false;
  }
}

export async function route() {
  if(!state.user||identityPending)return;const revision=++routeRevision;
  state.cleanup?.();state.cleanup=null;
  const id=(location.hash.slice(1).split('?')[0]||'overview');const selected=nav.find(item=>item[0]===id)||nav[0];state.view=selected[0];
  $('#pageTitle').textContent=t(selected[2]);document.title=html`${t(selected[2])} · 知序`;$('#sidebar').classList.remove('open');
  document.querySelectorAll('[data-nav]').forEach(link=>{const active=link.dataset.nav===state.view;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
  $('#view').innerHTML=staticHTML('<div class="loading">正在加载…</div>');
  try {
    if(!state.workspace){$('#view').innerHTML=staticHTML('<div class="empty"><strong>你还没有工作空间</strong>请创建组织，或使用管理员分享的邀请链接。</div>');return;}
    const module=await import(['overview','team','billing','settings'].includes(state.view)?'./views.js':`./${state.view}.js`);
    if(revision!==routeRevision)return;
    const outlet=document.createElement('div');
    const scopedClient=new Client(api.fetcher);scopedClient.setContext(state.workspace.id,state.csrf);
    const viewState={...state,workspace:{...state.workspace},user:{...state.user}};
    const isCurrent=()=>revision===routeRevision&&!identityPending;
    const cleanup=await module.render(outlet,{state:viewState,api:scopedClient,refreshIdentity:preferred=>refreshIdentity(preferred,isCurrent),route:()=>isCurrent()?route():undefined,isCurrent});
    if(revision===routeRevision){$('#view').replaceChildren(outlet);state.cleanup=cleanup;}else cleanup?.();
  }catch(error){if(revision!==routeRevision)return;if(error.status===401){authScreen();return;}$('#view').innerHTML=html`<div class="error-state"><strong>无法载入${esc(t(selected[2]))}</strong><p>${esc(error.message)}</p><button id="retryView" class="secondary">重新连接</button></div>`;$('#retryView').onclick=route;}
}

localizePage();
$('#workspace').onchange=event=>switchOrganization(event.target.value);
$('#mobileMenu').onclick=()=>$('#sidebar').classList.toggle('open');
$('#logoutButton').onclick=async()=>{const revision=invalidateView();try{await api.request('/api/v1/auth/logout',{method:'POST'});if(revision===routeRevision)authScreen();}catch(error){if(revision===routeRevision){toast(error.message,'error');await route();}}};
$('#accountButton').onclick=()=>{
  const dialog=showModal(t('账号与组织'),html`<p class="muted">${esc(state.user?.email)}</p><label>操作<select name="action"><option value="create">创建工作空间</option><option value="join">接受团队邀请</option></select></label><label data-create>新组织名称<input name="name" placeholder="创建另一个工作空间" required maxlength="128"></label><label data-join class="hidden">邀请令牌<textarea name="token" rows="3" minlength="32" maxlength="128"></textarea></label><p data-join class="hint hidden">登录邮箱必须与邀请邮箱一致；令牌使用后即失效。</p>`,async data=>{
    const result=data.action==='join'?await api.request('/api/v1/invitations/accept',{method:'POST',json:{token:data.token.trim()}}):await api.request('/api/v1/organizations',{method:'POST',json:{name:data.name}});
    if(await switchOrganization(result.id||result.organization?.id))toast(data.action==='join'?t('已加入团队'):t('工作空间已创建'),'success');
  });
  const form=$('form',dialog);form.elements.action.onchange=()=>{const join=form.elements.action.value==='join';form.querySelectorAll('[data-create]').forEach(item=>item.classList.toggle('hidden',join));form.querySelectorAll('[data-join]').forEach(item=>item.classList.toggle('hidden',!join));form.elements.name.required=!join;form.elements.token.required=join;};
};
window.addEventListener('hashchange',route);
document.addEventListener('click',event=>{if(window.innerWidth<=640&&!event.target.closest('#sidebar')&&!event.target.closest('#mobileMenu'))$('#sidebar').classList.remove('open');});

try{await refreshIdentity();showShell();await route();}catch(error){if(error.status===401)authScreen();else{$('#boot').innerHTML=html`<div class="brand-mark">知</div><p>无法连接工作台：${esc(error.message)}</p><button id="reconnect" class="secondary">重新连接</button>`;$('#reconnect').onclick=()=>location.reload();}}
