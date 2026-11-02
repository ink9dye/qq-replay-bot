const SS=window.StatisticsSamples,$=id=>document.getElementById(id),el=SS.el,args=new URLSearchParams(location.search),player=args.get('player')||'',digest=args.get('digest');let data=null,request=0,page=0;
function rankText(rank){return rank?'当前#'+rank:'当前排名暂缺'}
function cardImage(code,large=false){return 'https://cdn.233.momobako.com/ygopro/pics/'+code+(large?'.jpg!half':'.jpg!thumb2')}
function updateDownload(){if(!data)return;const q=new URLSearchParams({player,digest:data.selected.digest,inferred:'1'});$('download').href='/statistics/build-ydk?'+q;$('download').textContent='下载YDK'}
function cardTile(card,inferred=false){const box=el('div',undefined,'deck-card'+(inferred?' side-inferred':'')),img=el('img'),badge=el('span','×'+card.copies,'count');const local=cardImage(card.code),remote='https://cdntx.moecube.com/ygopro-super-pre/data/pics/'+card.code+'.jpg';img.src=local;img.referrerPolicy='no-referrer';img.decoding='async';img.alt=card.name+' #'+card.code;img.loading='lazy';img.onerror=()=>{if(!img.dataset.cdn){img.dataset.cdn='1';img.src=remote;return}const empty=el('div','暂无卡图','missing-image');img.replaceWith(empty)};box.append(img,badge,el('div',card.name+' #'+card.code,'caption'));box.tabIndex=0;box.setAttribute('role','button');box.setAttribute('aria-label','查看'+card.name+'的卡片信息');box.onclick=e=>{if(!e.target.closest('details,a'))showCard(card,inferred)};box.onkeydown=e=>{if(e.target===box&&(e.key==='Enter'||e.key===' ')){e.preventDefault();showCard(card,inferred)}};box.title=card.name+' #'+card.code+' ×'+card.copies;return box}
function renderDeck(){const container=$('deckCards');container.replaceChildren();for(const [part,label]of [['main','主卡'],['extra','额外'],['side','Side']]){const cards=data.cards.filter(c=>c.partition===part),section=el('section',undefined,'deck-section'),count=cards.reduce((n,c)=>n+c.copies,0);if(part==='side'&&!cards.length&&!data.completeness[part])continue;section.append(el('h3',`${label} · ${data.completeness[part]?count+'张（完整）':count+'张已记录（不完整／未知）'}`));const grid=el('div',undefined,'deck-grid');for(const c of cards)grid.append(cardTile(c));section.append(grid);if(!cards.length)section.append(el('p',data.completeness[part]?'已确认此分区为空。':'尚无直接记录，不能当作没有投入。','note'));container.append(section)}
const side=data.inferred_side,n=$('sideCandidates');n.replaceChildren();if(side.cards.length){n.append(el('h3','Side'));const grid=el('div',undefined,'deck-grid');for(const c of side.cards)grid.append(cardTile(c,true));n.append(grid)}updateDownload()}
function renderHistory(){
 const container=$('history');container.replaceChildren();const groups=data.match_groups||[];
 for(const group of groups.slice(page*12,(page+1)*12)){
  const section=el('section',undefined,'match-group');section.style.borderBottom='2px solid #dce4ef';section.style.marginBottom='24px';
  const firstGame=group.games[0],zip=el('a','下载整场已收录录像ZIP（'+group.games.length+'局）');zip.className='download';zip.href='/statistics/recording-zip?'+new URLSearchParams({player,digest:data.selected.digest,scope:data.scope,match:group.id});section.append(zip);section.append(el('h3',(group.associated?'同场对局':'未关联整场的小局')+' · '+SS.time(group.recorded_at)+' · 已收录'+group.games.length+'局'));
  if(group.missing_game_numbers.length)section.append(el('p','第'+group.missing_game_numbers.join('、')+'局当前无有效来源（可能未采集、已到期、被排除或文件缺失），不会补猜。','notice'));
  if(group.sequence_ambiguous)section.append(el('p','同一局次存在多个来源，换卡比较暂不确定。','notice'));
  for(const m of group.games){
   const row=el('div',undefined,'history-row'),left=el('div'),right=el('div',undefined,'links');
   left.append(el('strong',m.recording_title||`${rankText(m.current_rank)} ${m.player} vs ${m.opponent||'对手未知'}`),el('p',`${SS.time(m.recorded_at)} · ${m.game_no?'同场第'+m.game_no+'局':'局次未知'} · ${m.first_player===1?'本方先攻':m.first_player===0?'本方后攻':'先后攻未知'} · ${{win:'本方胜',loss:'本方负',draw:'平局',unknown:'胜负未知'}[m.outcome]} · ${m.matches_selected_build?'本页构筑':m.matches_selected_main?'同主卡构筑':'同场换边构筑'} · 当时排名未记录`,'note'));
   const change=m.changes||{},swap=el('div',undefined,'note');
   if(change.status==='known'||change.status==='partial'){
    swap.append(el('b','对比第'+change.from_game_no+'局的换卡'));
    for(const [key,label] of [['added','换入'],['removed','换出']]){const cards=change[key]||[];swap.append(el('p',label+'：'+(cards.map(c=>`${c.name} #${c.code} ×${c.copies}（${c.partition==='main'?'主卡':'额外'}）`).join('、')||'已知分区无变化')))}
    if(change.status==='partial')swap.append(el('p','部分分区卡表不完整，仅展示可确认的差异。'));
   }else swap.append(el('p',({first_game:'首局，无上一局可比较。',missing_first_game:'第一局无有效来源，换入／换出未知。',missing_previous_game:'上一局无有效来源，换入／换出未知。',ambiguous_sequence:'局次关联有冲突，换入／换出未知。',unassociated:'场次未可靠关联，换入／换出未知。'})[change.status]||'换入／换出未知。'));
   left.append(swap);const replay=el('a','下载第'+(m.game_no||'?')+'局录像'),build=el('a','查看当场构筑');replay.className='download';replay.href='/statistics/sample/'+m.digest+'?player='+encodeURIComponent(player);build.className='download';build.href='/statistics/build?'+new URLSearchParams({player,digest:m.digest});right.append(replay,build);row.append(left,right);section.append(row);
  }
  container.append(section);
 }
 if(!groups.length)container.append(el('p','当前范围暂无有效小局。'));
 const nav=$('pager'),back=el('button','上一页','secondary'),next=el('button','下一页','secondary');nav.replaceChildren();back.disabled=page===0;next.disabled=(page+1)*12>=groups.length;back.onclick=()=>{page--;renderHistory()};next.onclick=()=>{page++;renderHistory()};nav.append(back,el('span',`${groups.length?page+1:0}/${Math.ceil(groups.length/12)} · ${groups.length}组／${data.matches.length}小局`,'note'),next)
}

function render(r){data=r;document.title=r.person.player+' · 构筑与对局';$('player').textContent=r.person.player;$('rank').textContent=rankText(r.person.rank);$('deckName').textContent=r.person.deck_type||'卡组未识别';$('points').textContent=r.person.points===null?'暂无当前积分':Number(r.person.points).toLocaleString('zh-CN',{maximumFractionDigits:2});$('downloadAll').href='/statistics/recording-zip?'+new URLSearchParams({player,digest:r.selected.digest,scope:r.scope});$('gameCount').textContent=r.matches.length;$('gameLabel').textContent=r.scope==='build'?'涉及整场的已收录小局':'该玩家已收录小局';const win=r.scope_win_rate;$('updated').textContent='构筑观测：'+SS.time(r.selected.recorded_at);$('status').textContent=`榜单${r.roster.version}${r.roster.stale?'（过期，最近成功名单）':''} · ${SS.time(r.roster.updated_at)} · 数据原期限内有效`;$('sourceMeta').textContent=`来源 ${r.selected.digest.slice(0,12)} · 原到期 ${SS.time(r.selected.expires_at)} · ${r.selected.observation_kind==='confirmed_first_game'?'已确认首局':r.selected.observation_kind==='post_side'?'换边后观测':'首局状态未知／历史观测'}`;$('historyMeta').textContent=`当前范围${r.matches.length}局（其中主卡相同${r.same_main_games}局） · 胜${win.wins}／负${win.losses}／平${win.draws} · 胜负未知${win.unknown_samples}；该玩家全部有效记录共${r.all_player_games}局。`;renderDeck();page=0;renderHistory()}
async function load(){if(!player){$('status').textContent='请从玩家列表或小局来源打开构筑。';return}const id=++request,q=new URLSearchParams({player,scope:$('scope').value});if(digest)q.set('digest',digest);try{const r=await SS.api('build-detail?'+q);if(id!==request)return;if(!r){$('status').textContent='构筑来源已到期、被排除或玩家已不在当前榜单。';return}render(r)}catch(e){$('status').textContent=e.message}}
$('scope').onchange=load;$('copy').onclick=async()=>{if(!data)return;const url=location.href;try{await navigator.clipboard.writeText(url);$('copy').textContent='已复制本页链接'}catch(e){$('copy').textContent='请复制地址栏中的链接'}};load();

let cardRequest=0;
async function showCard(card,inferred=false){const id=++cardRequest,dialog=$('cardDialog'),body=$('cardContent');body.replaceChildren(el('p','读取卡片信息…'));dialog.showModal();try{const info=await SS.api('card-info?code='+card.code);if(id!==cardRequest||!dialog.open)return;const img=el('img'),text=el('div'),title=el('h2',info.name);title.id='cardTitle';img.src=cardImage(card.code,true);img.referrerPolicy='no-referrer';img.decoding='async';img.alt=info.name;img.onerror=()=>img.replaceWith(el('p','本地暂无卡图'));text.append(title,el('p','#'+info.code+' · '+(info.base_partition==='extra'?'额外卡组卡片':info.base_partition==='main'?'主卡组卡片':'卡片类型暂无资料'),'note'),el('p',info.description||'本地暂无效果描述。','card-description'));if(inferred||card.partition==='side'){const scenes=el('section');scenes.append(el('h3','换入场景'));const contexts=card.swap_contexts||[];if(!contexts.length)scenes.append(el('p','暂无可确认的相邻小局换入记录，不能由Side投入反推换入。','note'));for(const c of contexts){const row=el('div'),link=el('a','下载第'+c.game_no+'局源录像');row.append(el('p',`${c.kind==='same_match'?'同场换边':'历史同构筑'} · 对战${c.opponent_deck_type||'卡组未知'} · ${c.first_player===1?'我方先攻':c.first_player===0?'我方后攻':'先后攻未知'} · 第${c.game_no}局换入${c.copies}张（${c.partition==='main'?'主卡':'额外'}）`));link.href='/statistics/sample/'+c.digest+'?player='+encodeURIComponent(player);row.append(link);scenes.append(row)}text.append(scenes)}body.replaceChildren(img,text)}catch(e){body.replaceChildren(el('p',e.message))}}
$('closeCard').onclick=()=>{$('cardDialog').close();cardRequest++};
// /stats/ 分享页：录像 ZIP 由分享服务单独处理；失败时在页面内给出明确错误，
// 避免浏览器直接导航到空白响应。成功时仍按原文件名下载。
if(location.pathname.startsWith('/stats/')){
document.addEventListener('click',async event=>{
 const link=event.target.closest&&event.target.closest('a[href*="recording-zip"]');
 if(!link)return;
 event.preventDefault();
 const status=$('status'),old=status.textContent;
 status.textContent='正在打包录像…';
 try{
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
  let response;
  try{response=await fetch(link.href,{credentials:'same-origin',signal:controller.signal});}
  finally{clearTimeout(timer);}
  if(!response.ok){
   const text=(await response.text()).slice(0,200);
   status.textContent='录像打包失败：'+(text||('HTTP '+response.status));
   return;
  }
  const blob=await response.blob();
  const disposition=response.headers.get('Content-Disposition')||'';
  const match=/filename\*=UTF-8''([^;]+)/.exec(disposition);
  const filename=match?decodeURIComponent(match[1]):'replays.zip';
  const url=URL.createObjectURL(blob),anchor=document.createElement('a');
  anchor.href=url;anchor.download=filename;document.body.append(anchor);anchor.click();anchor.remove();
  setTimeout(()=>URL.revokeObjectURL(url),10000);
  status.textContent=old;
 }catch(error){
  status.textContent=error&&error.name==='AbortError'?'录像打包超时（服务端未返回数据），请稍后重试。':'录像打包暂时不可用（服务端未返回数据），请稍后重试。';
 }
});
}
