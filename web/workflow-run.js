const terminalStates=new Set(['completed','degraded','insufficient_evidence','error','canceled','timed_out']);
export const terminalRun=run=>terminalStates.has(run?.status);

function pause(milliseconds,signal){
  if(!milliseconds||signal?.aborted)return Promise.resolve();
  return new Promise(resolve=>{
    const finish=()=>{clearTimeout(timer);signal?.removeEventListener('abort',finish);resolve();};
    const timer=setTimeout(finish,milliseconds);signal?.addEventListener('abort',finish,{once:true});
  });
}

export async function runWorkflow(api,workflowId,body,{signal,onUpdate=()=>{},pollInterval=500,maxWaitMs=360000}={}){
  if(signal?.aborted)throw new DOMException('已取消启动工作流。','AbortError');
  const base=`/api/v1/workflows/${encodeURIComponent(workflowId)}`;
  // Keep the creation response so an early cancel cannot orphan an accepted run.
  let run=await api.request(`${base}/run`,{method:'POST',json:body});
  if(!run?.id)throw new Error('服务器未返回运行编号，请刷新运行记录核对状态。');
  const path=`${base}/runs/${encodeURIComponent(run.id)}`,deadline=Date.now()+maxWaitMs;
  let cancellationSent=false;
  onUpdate(run);
  try{
    while(!terminalRun(run)){
      if(signal?.aborted&&!cancellationSent){
        cancellationSent=true;
        run=await api.request(`${path}/cancel`,{method:'POST',json:{}});
        onUpdate(run);if(terminalRun(run))break;
      }
      if(Date.now()>=deadline)throw new Error('运行状态尚未确认，请刷新服务器运行记录。');
      await pause(pollInterval,cancellationSent?undefined:signal);
      // Cancellation still polls for the server-confirmed terminal state.
      run=await api.request(path);onUpdate(run);
    }
    return run;
  }catch(error){error.runId=run.id;throw error;}
}
