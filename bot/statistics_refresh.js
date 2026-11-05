/* Preserve successful results while one bounded server refresh runs. */
(function () {
  'use strict';
  const native = window.fetch.bind(window);
  let generation=0;
  function fingerprint(){return [...document.querySelectorAll('input,select')].map(n=>['checkbox','radio'].includes(n.type)?n.checked:n.value).join('\u0001')}
  function notice(text) {
    let n=document.getElementById('statistics-refresh-note');
    if(!n){n=document.createElement('p');n.id='statistics-refresh-note';n.setAttribute('role','status');
      n.style.cssText='padding:10px 14px;background:#fff5df;color:#79541b;border-radius:8px';
      const host=document.querySelector('main')||document.body;host.prepend(n)}
    n.hidden=!text;n.textContent=text;
  }
  function pause(ms,signal){return new Promise((resolve,reject)=>{
    const abort=()=>{clearTimeout(timer);reject(new DOMException('已取消','AbortError'))};
    const timer=setTimeout(()=>{if(signal)signal.removeEventListener('abort',abort);resolve()},ms);
    if(signal){if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true})}
  })}
  window.fetch=async function(input,options){
    const raw=typeof input==='string'?input:input.url;
    const url=new URL(raw,location.href),kind=url.pathname.split('/').pop();
    if(url.origin!==location.origin||!['roster','recordings','card-report'].includes(kind))return native(input,options);
    const signal=(options&&options.signal)||(input&&input.signal);
    const current=++generation,form=fingerprint();
    for(let attempt=0;;attempt++){
      const response=await native(input,options);
      if(response.status===503&&attempt<12){
        let payload;try{payload=await response.clone().json()}catch(_){}
        if(payload&&payload.code==='query_busy'){
          notice('正在更新统计，已显示的结果会保留，请稍候…');
          await pause(3000,signal);continue;
        }
      }
      const state=response.headers.get('X-Statistics-State');
      if(state==='refreshing'){
        const stamp=Number(response.headers.get('X-Statistics-Saved-At'));
        notice('正在更新，当前显示 '+new Date(stamp*1000).toLocaleString('zh-CN')+' 保存的上次统计结果。');
        (async()=>{
          for(let n=0;n<20;n++){
            await pause(3000,signal);
            if(current!==generation||document.hidden)return;
            const next=await native(input,options);
            if(next.ok&&Number(next.headers.get('X-Statistics-Saved-At'))>stamp){
              notice('统计已更新。');
              if(form===fingerprint())window.dispatchEvent(new CustomEvent('statistics-result-ready',{detail:{kind}}));
              else notice('统计已更新；筛选条件已改，请点击查询查看。');
              return;
            }
          }
        })().catch(()=>{});
      }else if(response.ok)notice('');
      return response;
    }
  };
})();
