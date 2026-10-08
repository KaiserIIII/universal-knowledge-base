import {english} from './i18n-en.js';
const supported=new Set(['zh-CN','en']);
export const LOCALE_KEY='knowledge-workspace.locale';
let locale='zh-CN';
const escapeRegex=value=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const fragmentPattern=new RegExp(Object.keys(english).sort((a,b)=>b.length-a.length).map(escapeRegex).join('|'),'g');
export const getLocale=()=>locale;
export function setLocale(value){if(!supported.has(value))return false;locale=value;if(globalThis.document?.documentElement)document.documentElement.lang=locale;return true;}
function browserStorage(){try{return globalThis.localStorage;}catch{return null;}}
export function initializeLocale({url=globalThis.location?.href,storage=browserStorage()}={}){
  let selected=null;try{selected=new URL(url).searchParams.get('lang');}catch{}
  if(!supported.has(selected)){try{selected=storage?.getItem(LOCALE_KEY);}catch{selected=null;}}
  setLocale(supported.has(selected)?selected:'zh-CN');return locale;
}
// t is for authored static strings. Callers must never pass user or API content.
export function t(value){return locale==='en'?(english[value]??value):value;}
function fragment(value){return locale==='en'?value.replace(fragmentPattern,key=>english[key]):value;}
// Only template quasis are translated. Values have already been escaped by callers.
export function html(strings,...values){return strings.reduce((result,part,index)=>result+fragment(part)+(index<values.length?values[index]??'':''),'');}
export function staticHTML(value){return fragment(value);}
export function localeURL(path,base=globalThis.location?.href||'http://localhost/'){
  const url=new URL(path,base);url.searchParams.set('lang',locale);return url.href;
}
export function switchLocale(value,{location=globalThis.location,storage=browserStorage(),confirm=message=>globalThis.window.confirm(message)}={}){
  if(!supported.has(value)||value===locale)return false;
  const message=locale==='en'?'Switch language and reload? Unsaved edits will be discarded and requests in progress may be interrupted. Your current page will be preserved.':'切换语言并重新加载？未保存的编辑会丢失，进行中的请求可能中断。当前页面位置会保留。';
  if(!confirm(message))return false;
  const url=new URL(location.href);url.searchParams.set('lang',value);
  try{storage?.setItem(LOCALE_KEY,value);}catch{}
  location.assign(url.href);return true;
}
export function bindLanguageControl(control){if(!control)return;control.value=locale;control.onchange=()=>{if(!switchLocale(control.value))control.value=locale;};}
// This opt-in selector only targets authored static text and attributes in static pages.
export function localizePage(root=globalThis.document){
  if(!root?.querySelectorAll)return;
  for(const node of root.querySelectorAll('[data-i18n]'))node.textContent=t(node.getAttribute('data-i18n'));
  for(const attr of ['placeholder','aria-label','content'])for(const node of root.querySelectorAll(`[data-i18n-${attr}]`))node.setAttribute(attr,t(node.getAttribute(`data-i18n-${attr}`)));
  for(const node of root.querySelectorAll('[data-locale-link]'))node.href=localeURL(node.getAttribute('href'));
  root.querySelectorAll('[data-language-control]').forEach(bindLanguageControl);
}
initializeLocale();
