import {t,html,staticHTML,localizePage,bindLanguageControl,localeURL,getLocale} from './i18n.js';
export class APIError extends Error {
  constructor(message, status) { super(message); this.status=status; }
}

const friendlyErrors={
  'Billing is not configured':'支付尚未配置，请联系部署管理员。',
  'Authentication required':'请登录后继续；会话可能已经过期。',
  'CSRF token required':'会话校验失败，请刷新页面后重试。',
  'Resource not found':'资源不存在，或当前组织没有访问权限。',
  'Invitation not found':'邀请不存在、已过期或已被使用，请向管理员索取新邀请。',
  'Local retrieval dependencies unavailable':'本地检索依赖尚未安装，请联系部署管理员。',
  'Local reranking capability is disabled':'部署尚未启用重排，请关闭此选项或联系管理员。',
  'Model credential reference is not configured':'模型凭据尚未注入服务环境，请联系部署管理员配置后重试。',
  'Unsupported or unauthorized model configuration':'模型协议、地址或凭据引用不在部署允许范围内，请核对配置。',
  'Invalid or oversized graph configuration':'工作流参数格式错误或超过大小限制，请核对节点配置。',
  'Invalid, duplicate or incompatible graph edge':'工作流包含无效、重复或不兼容的连线，请核对端口。',
  'Exactly one input and output are required':'工作流需要且只能有一个问题输入与回答输出。',
  'Every node must connect input to output':'每个模块都需要位于问题输入到回答输出的路径上。',
  'Graph contains a cycle':'工作流存在循环连线，请调整连接。',
  'Select a knowledge base':'请为检索模块选择知识库。',
  'Select a model profile':'请为模型调用模块选择模型连接。',
  'Workflow model or output-token budget exceeded':'模型调用次数或总输出 Token 超出运行预算，请减少分支或调整参数。',
  'Local parsing capability unavailable':'本地解析能力暂不可用，请检查部署依赖。',
  'The last owner cannot be removed or demoted':'请保留至少一位所有者，再调整此成员权限。',
};

export class Client {
  constructor(fetcher=globalThis.fetch.bind(globalThis)) { this.fetcher=fetcher; this.workspaceId=null; this.csrf=null; }
  setContext(workspaceId,csrf) { this.workspaceId=workspaceId; this.csrf=csrf; }
  async response(path,options={}) {
    const {json,...init}=options;
    const headers=new Headers(init.headers);
    const method=(init.method||'GET').toUpperCase();
    if(this.workspaceId) headers.set('X-Workspace-ID',this.workspaceId);
    if(this.csrf&&!['GET','HEAD','OPTIONS'].includes(method)) headers.set('X-CSRF-Token',this.csrf);
    if(json!==undefined) { headers.set('Content-Type','application/json'); init.body=JSON.stringify(json); }
    const response=await this.fetcher(path,{...init,method,headers,credentials:'same-origin'});
    if(!response.ok) {
      let message=html`请求失败 (${response.status})`;
      try {const data=await response.json();const detail=data.detail;if(detail?.code==='quota_exceeded')message=html`${({members:t('团队成员'),knowledge_bases:t('知识库'),documents:t('文档'),monthly_answers:t('本月问答')})[detail.resource]||detail.resource}已达到套餐上限（${detail.limit}），请清理资源或调整订阅`;else if(Array.isArray(detail))message=detail.map(item=>`${(item.loc||[]).filter(part=>part!=='body').join('.')}: ${item.msg}`).join('；');else message=typeof detail==='string'?detail:JSON.stringify(detail||data.message||message);} catch {}
      throw new APIError(t(friendlyErrors[message])||message,response.status);
    }
    return response;
  }
  async request(path,options={}) {
    const response=await this.response(path,options);
    return response.status===204?null:response.json();
  }
  async *stream(path,options={}) {
    const response=await this.response(path,options);
    if(!response.body) throw new APIError(t('服务器没有返回数据流'),502);
    const reader=response.body.getReader();
    async function* source() { try {while(true) {const {value,done}=await reader.read();if(done)return;yield value;}} finally {await reader.cancel().catch(()=>{});reader.releaseLock();} }
    yield* readSSE(source());
  }
}

export async function* readSSE(chunks) {
  const decoder=new TextDecoder(); let buffer='';
  const parse=frame=>{
    const data=frame.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
    if(!data)return null;
    if(data.trim()==='[DONE]')return 'done';
    try {return JSON.parse(data);} catch {throw new APIError(t('服务器返回了无法解析的流事件'),502);}
  };
  for await (const chunk of chunks) {
    buffer+=typeof chunk==='string'?chunk:decoder.decode(chunk,{stream:true});
    if(buffer.length>2_000_000)throw new APIError(t('数据流事件超过大小限制'),502);
    let match;
    while((match=/\r?\n\r?\n/.exec(buffer))) {
      const frame=buffer.slice(0,match.index); buffer=buffer.slice(match.index+match[0].length);
      const event=parse(frame); if(event==='done')return; if(event)yield event;
    }
  }
  buffer+=decoder.decode();
  if(buffer.trim()) {const event=parse(buffer);if(event&&event!=='done')yield event;}
}

export const api=new Client();
