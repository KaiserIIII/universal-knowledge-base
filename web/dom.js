import {t,html,staticHTML,localizePage,bindLanguageControl,localeURL,getLocale} from './i18n.js';
export const $=(selector,root=document)=>root.querySelector(selector);
export const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export function el(tag,attributes={},children=[]) {
  const node=document.createElement(tag);
  for(const [name,value]of Object.entries(attributes)) {
    if(name==='text')node.textContent=value;
    else if(name.startsWith('on'))node.addEventListener(name.slice(2).toLowerCase(),value);
    else if(value!==null&&value!==undefined)node.setAttribute(name,String(value));
  }
  for(const child of children)node.append(child);
  return node;
}
export function toast(message,kind='info') {
  const host=$('#toasts');if(!host)return;
  const item=el('div',{class:`toast ${kind}`,role:'status',text:message});host.append(item);setTimeout(()=>item.remove(),6500);
}
export const when=value=>value?new Date(value).toLocaleString(getLocale(),{dateStyle:'short',timeStyle:'short'}):'—';
export function formJSON(form){return Object.fromEntries(new FormData(form).entries());}
export function download(name,content,type='text/plain') {
  if(typeof content==='object'){content=JSON.stringify(content,null,2);type='application/json';}
  const url=URL.createObjectURL(new Blob([content],{type}));const a=el('a',{href:url,download:name});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
export function showModal(title,body,onSubmit) {
  const dialog=$('#modal');dialog.innerHTML=html`<form method="dialog"><header><h2>${esc(title)}</h2><button type="button" class="icon-btn" aria-label="关闭">×</button></header><div class="modal-body">${body}<div class="modal-error error-state hidden" role="alert"></div></div><footer><button type="button" class="quiet">取消</button><button class="primary" type="submit">确定</button></footer></form>`;
  dialog.querySelectorAll('button[type=button]').forEach(button=>button.onclick=()=>dialog.close());
  const form=$('form',dialog);form.onsubmit=async event=>{event.preventDefault();const submit=$('[type=submit]',form),errorBox=$('.modal-error',form);submit.disabled=true;errorBox.classList.add('hidden');try{await onSubmit(formJSON(form),form);dialog.close();}catch(error){errorBox.textContent=error.message;errorBox.classList.remove('hidden');errorBox.scrollIntoView({block:'nearest'});}finally{submit.disabled=false;}};
  dialog.showModal();return dialog;
}
