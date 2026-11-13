window.StatisticsSamples=(()=>{
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n},time=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false});
async function api(path){
 const candidate=path==='card-options'||path==='card-table'||path==='groups'||(path.startsWith('groups?')&&new URLSearchParams(path.split('?')[1]).get('candidates')==='1');
 const deadline=Date.now()+600000;
 for(let attempt=0;;attempt++){
  const r=await fetch('/statistics/api/'+path,{cache:'no-store',credentials:'same-origin'});
  if(candidate&&r.status===503&&Date.now()<deadline){
   let data;try{data=await r.clone().json()}catch(_){}
   if(data&&data.code==='query_busy'){
    await new Promise(resolve=>setTimeout(resolve,Math.min(30000,5000*Math.pow(1.5,Math.min(attempt,5)))));continue;
   }
  }
  if(!r.ok)throw Error(r.status===403?'访问已到期，请从本机入口重新打开。':await r.text());return r.json();
 }
}
function modal(title){const d=el('dialog'),close=el('button','关闭','close');close.onclick=()=>d.close();d.append(close,el('h2',title));document.body.append(d);d.addEventListener('close',()=>d.remove());d.showModal();return d}
async function show(sample,changed){const d=modal(sample.player+' · 小局来源'),status=el('p','读取中…','note');const page=el('a','打开完整构筑与对局记录','download');page.href='/statistics/build?player='+encodeURIComponent(sample.player)+'&digest='+sample.digest;d.append(page,status);try{const b=await api('sample?player='+encodeURIComponent(sample.player)+'&digest='+sample.digest);if(!d.open)return;if(b.recording_title)d.querySelector('h2').textContent=b.recording_title;const outcome={win:'本方胜',loss:'本方负',draw:'平局',unknown:'胜负未知'},first=b.source.first_player===1?'本方先攻':b.source.first_player===0?'本方后攻':'先后攻未知';status.textContent=`${b.person.deck_type||'卡组未识别'} · ${outcome[b.source.outcome]} · ${first} · ${time(b.source.recorded_at)} · 原到期 ${time(b.source.expires_at)}`;
for(const [part,label]of [['main','主卡'],['extra','额外'],['side','Side']]){const cards=b.cards.filter(c=>c.partition===part);d.append(el('h3',label+' · '+(b.completeness[part]?cards.reduce((n,c)=>n+c.copies,0)+'张（完整）':'不完整／未知')));const grid=el('div',undefined,'cards');for(const c of cards)grid.append(el('div','×'+c.copies+' '+c.name+' #'+c.code,'card'));d.append(grid)}
const download=el('a','下载对应源录像','download');download.href='/statistics/sample/'+sample.digest+'?player='+encodeURIComponent(sample.player);d.append(download);
}catch(e){status.textContent=e.message}}
function list(container,samples,changed){let page=0;function render(){container.replaceChildren();const limit=25,items=samples.slice(page*limit,(page+1)*limit);for(const s of items){const row=el('div',undefined,'sample-row'),b=el('button',s.recording_title||s.player+' · '+(s.deck_type||'卡组未识别')+' · '+time(s.recorded_at),'textbutton');b.onclick=()=>show(s,changed);row.append(b,el('span',({win:'胜',loss:'负',draw:'平',unknown:'未知'})[s.outcome]+' · '+(s.first_player===1?'先攻':s.first_player===0?'后攻':'先后攻未知'),'note'));container.append(row)}const nav=el('div',undefined,'pagination'),back=el('button','上一页','secondary'),next=el('button','下一页','secondary');back.disabled=page===0;next.disabled=(page+1)*limit>=samples.length;back.onclick=()=>{page--;render()};next.onclick=()=>{page++;render()};nav.append(back,el('span',` ${samples.length? page+1:0}/${Math.ceil(samples.length/limit)} · ${samples.length}视角 `,'note'),next);container.append(nav)}render()}
return {api,show,list,el,time};})();
// /stats/<token> 分享页：把相对 /stats/... 链接写成当前入口的绝对 URL。
// 从公网域名打开时即 https://<公网域名>/stats/...；本机测试打开时是当前本机地址。
if(location.pathname.startsWith('/stats/')){
 const absolutize=()=>{
  for(const link of document.querySelectorAll('a[href^="/stats/"]')){
   link.setAttribute('href',new URL(link.getAttribute('href'),location.origin).href);
  }
 };
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',absolutize);
 else absolutize();
 new MutationObserver(absolutize).observe(document.documentElement,{subtree:true,childList:true});
}
