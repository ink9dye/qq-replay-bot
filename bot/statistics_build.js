const SS=window.StatisticsSamples,$=id=>document.getElementById(id),el=SS.el,args=new URLSearchParams(location.search),player=args.get('player')||'',digest=args.get('digest');let data=null,request=0,page=0;
function rankText(rank){return rank?'当前#'+rank:'当前排名暂缺'}
function cardImage(code,large=false){return large?'/statistics/card-image/'+code:'/statistics/card-thumb/'+code}
function cardImageRemote(code){return code>=100000000?'https://cdntx.moecube.com/ygopro-super-pre/data/pics/'+code+'.jpg':'https://cdn.233.momobako.com/ygopro/pics/'+code+'.jpg!half'}
function updateDownload(){if(!data)return;const side=data.inferred_side||{},inferred=!!(side.cards&&side.cards.length)&&!side.conflict,q=new URLSearchParams({player,digest:data.selected.digest,inferred:inferred?'1':'0'});$('download').href='/statistics/build-ydk?'+q;$('download').textContent=inferred?'下载YDK（含推测Side）':'下载YDK（仅原始记录）'}
function cardTile(card,inferred=false){const box=el('div',undefined,'deck-card'+(inferred?' side-inferred':'')),img=el('img'),badge=el('span','×'+card.copies,'count');const local=cardImage(card.code),remote='https://cdntx.moecube.com/ygopro-super-pre/data/pics/'+card.code+'.jpg';img.src=local;img.referrerPolicy='no-referrer';img.decoding='async';img.alt=card.name+' #'+card.code;img.loading='lazy';img.onerror=()=>{if(!img.dataset.cdn){img.dataset.cdn='1';img.src=remote;return}const empty=el('div','暂无卡图','missing-image');img.replaceWith(empty)};box.append(img,badge,el('div',card.name+' #'+card.code,'caption'));box.tabIndex=0;box.setAttribute('role','button');box.setAttribute('aria-label','查看'+card.name+'的卡片信息');box.onclick=e=>{if(!e.target.closest('details,a'))showCard(card,inferred)};box.onkeydown=e=>{if(e.target===box&&(e.key==='Enter'||e.key===' ')){e.preventDefault();showCard(card,inferred)}};box.title=card.name+' #'+card.code+' ×'+card.copies;return box}

function renderValidation(){
 const box=$('buildValidation');if(!box)return;
 const validation=data.build_validation||{},issues=validation.issues||[];
 box.hidden=false;
 box.className=issues.length?'notice':'note';
 const conflict=!!(data.inferred_side&&data.inferred_side.cards&&data.inferred_side.cards.length&&data.inferred_side.conflict);
 box.textContent=(validation.summary||(issues.length?'构筑校验发现基础张数冲突。':'实体张数检查通过。')+'禁限表未校验（缺少对应版本）。')
  +(conflict?'推测 Side 存在冲突，未并入 YDK；页面上仍单独列出候选，供核对。':'');
}

function renderDeck(){renderValidation();const container=$('deckCards');container.replaceChildren();for(const [part,label]of [['main','主卡'],['extra','额外'],['side','Side']]){const cards=data.cards.filter(c=>c.partition===part),section=el('section',undefined,'deck-section'),count=cards.reduce((n,c)=>n+c.copies,0);if(part==='side'&&!cards.length&&!data.completeness[part])continue;section.append(el('h3',`${label} · ${data.completeness[part]?count+'张（完整）':count+'张已记录（不完整／未知）'}`));const grid=el('div',undefined,'deck-grid');for(const c of cards)grid.append(cardTile(c));section.append(grid);if(!cards.length)section.append(el('p',data.completeness[part]?'已确认此分区为空。':'尚无直接记录，不能当作没有投入。','note'));container.append(section)}
const side=data.inferred_side,n=$('sideCandidates');n.replaceChildren();if(side.cards.length){n.append(el('h3','Side（推测）'));const grid=el('div',undefined,'deck-grid');for(const c of side.cards)grid.append(cardTile(c,true));n.append(grid)}updateDownload()}
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

function render(r){data=r;document.title=r.person.player+' · 构筑与对局';$('player').textContent=r.person.player;$('rank').textContent=rankText(r.person.rank);$('deckName').textContent=r.person.deck_type||'卡组未识别';$('points').textContent=r.person.points===null?'暂无当前积分':Number(r.person.points).toLocaleString('zh-CN',{maximumFractionDigits:2});$('downloadAll').href='/statistics/recording-zip?'+new URLSearchParams({player,digest:r.selected.digest,scope:r.scope});$('gameCount').textContent=r.matches.length;$('gameLabel').textContent=r.scope==='build'?'涉及整场的已收录小局':'该玩家已收录小局';const win=r.scope_win_rate;$('updated').textContent='构筑观测：'+SS.time(r.selected.recorded_at);$('status').textContent=`榜单${r.roster.version}${r.roster.stale?'（过期，最近成功名单）':''} · ${SS.time(r.roster.updated_at)} · 数据原期限内有效`;$('sourceMeta').textContent=`来源 ${r.selected.digest.slice(0,12)} · 原到期 ${SS.time(r.selected.expires_at)} · ${r.selected.observation_kind==='confirmed_first_game'?'已确认首局':r.selected.observation_kind==='post_side'?'换边后观测':'首局状态未知／历史观测'}`;$('historyMeta').textContent=`当前范围${r.matches.length}局（其中主卡相同${r.same_main_games}局） · 胜${win.wins}／负${win.losses}／平${win.draws} · 胜负未知${win.unknown_samples}；该玩家全部有效记录共${r.all_player_games}局。`;renderDeck();page=0;renderHistory();recentData=null;loadSideView()}
function statusNote(text){const box=$('status');box.className='note';box.textContent=text}
function cleanError(error){const text=String((error&&error.message)||'').trim();return !text||text.startsWith('<')?'统计服务暂时不可用，请稍后刷新重试。':text}
function statusProblem(nodes){const box=$('status');box.className='build-problem';box.replaceChildren(...nodes);box.scrollIntoView({block:'nearest'})}
function unavailableNodes(){
 const share=location.pathname.startsWith('/s/'),items=digest?[
  '这局已超过 14 天保留期：到期后原始录像和构筑记录会被删除，不能补查。',
  '这局的结果或局次还没确认，暂时被统计质量过滤排除（刚打完的局常见，等 1–2 分钟再刷新）。',
  '这个来源编号不属于当前玩家，或该局已被排除。'
 ]:[
  '这名玩家最近 14 天内没有可用的采集小局（没排到、录像未上传，或记录已到期）。',
  '已采集的小局结果或局次还没确认，暂时被质量过滤排除（刚打完的局常见，等 1–2 分钟再刷新）。',
  share?'本分享链接的授权范围不包含这名玩家的对局。':'当前链接的玩家名与统计库记录不一致，建议从玩家列表重新进入。'
 ];
 const list=el('ul');for(const text of items)list.append(el('li',text));
 const retry=el('button','重新读取');retry.className='secondary';retry.onclick=()=>load();
 return [el('strong','这份构筑现在打不开'),el('p','常见原因（满足任一都会这样）：'),list,
  el('p','保留期只有 14 天，网页不保存更早的构筑；需要留档请当场下载 YDK 或整场录像。','hint'),retry]
}
async function load(){if(!player){statusNote('请从玩家列表或小局来源打开构筑。');return}const id=++request,q=new URLSearchParams({player,scope:$('scope').value});if(digest)q.set('digest',digest);statusNote('读取构筑中…');try{const r=await SS.api('build-detail?'+q);if(id!==request)return;if(!r){statusProblem(unavailableNodes());return}render(r)}catch(e){statusProblem([el('strong','构筑读取失败'),el('p',cleanError(e)),el('p','请稍后刷新；如果一直失败，请重新获取统计或分享链接。','hint')])}}
$('scope').onchange=load;$('copy').onclick=async()=>{if(!data)return;const url=location.href;try{await navigator.clipboard.writeText(url);$('copy').textContent='已复制本页链接'}catch(e){$('copy').textContent='请复制地址栏中的链接'}};load();

let cardRequest=0;
async function showCard(card,inferred=false){const id=++cardRequest,dialog=$('cardDialog'),body=$('cardContent');body.replaceChildren(el('p','读取卡片信息…'));dialog.showModal();try{const info=await SS.api('card-info?code='+card.code);if(id!==cardRequest||!dialog.open)return;const img=el('img'),text=el('div'),title=el('h2',info.name);title.id='cardTitle';const sources=[cardImage(card.code,true),cardImageRemote(card.code),'/statistics/card-thumb/'+card.code];let sourceIndex=0;img.referrerPolicy='no-referrer';img.decoding='async';img.alt=info.name;img.onerror=()=>{sourceIndex+=1;if(sourceIndex<sources.length){img.src=sources[sourceIndex];return}img.replaceWith(el('p','暂无卡图'))};img.src=sources[0];text.append(title,el('p','#'+info.code+' · '+(info.base_partition==='extra'?'额外卡组卡片':info.base_partition==='main'?'主卡组卡片':'卡片类型暂无资料'),'note'),el('p',info.description||'本地暂无效果描述。','card-description'));if(inferred||card.partition==='side'){const scenes=el('section');scenes.append(el('h3','换入场景'));const contexts=card.swap_contexts||[];if(!contexts.length)scenes.append(el('p','暂无可确认的相邻小局换入记录，不能由Side投入反推换入。','note'));for(const c of contexts){const row=el('div'),link=el('a','下载第'+c.game_no+'局源录像');row.append(el('p',`${c.kind==='same_match'?'同场换边':'历史同构筑'} · 对战${c.opponent_deck_type||'卡组未知'} · ${c.first_player===1?'我方先攻':c.first_player===0?'我方后攻':'先后攻未知'} · 第${c.game_no}局换入${c.copies}张（${c.partition==='main'?'主卡':'额外'}）`));link.href='/statistics/sample/'+c.digest+'?player='+encodeURIComponent(player);row.append(link);scenes.append(row)}text.append(scenes)}body.replaceChildren(img,text)}catch(e){body.replaceChildren(el('p',e.message))}}
$('closeCard').onclick=()=>{$('cardDialog').close();cardRequest++};

// Side 对阵分析：复用玩家排行 -> 构筑入口，按对手卡组筛选逐场换边证据。
const SIDE_STATUS={observed:'有明确换边证据',no_later_games:'只有首局，缺少换边局',missing_first_game:'缺少可靠首局',ambiguous_sequence:'局次关联冲突',identity_unreliable:'场次身份不可靠',partial_build:'卡表不完整',missing_build:'缺少当场卡表',pair_conflict:'双方玩家关联不唯一',unassociated:'场次未可靠关联'};
const SIDE_REASON={first_game_unavailable:'无唯一可靠首局',direct_side_complete:'已直接记录 Side',selected_build_incomplete:'当前构筑不完整',no_unique_first_game_in_scope:'本方向无唯一首局',observed_game1_delta_support_with_latent_not_full_side:'既有支持度筛选',conflict:'候选冲突'};
let sideRequest=0,sideOptionsDone=false,sideScope='build',sideWindow='recent',recentData=null;
function sideName(code,name){return name||('#'+code)}
function sidePct(card){return card.percentage===null||card.percentage===undefined?'—':card.percentage.toFixed(1)+'%'}
function sideCopies(card){const parts=Object.entries(card.copies||{}).map(([n,count])=>`${n}张×${count}场`);return parts.length?parts.join('／'):'—'}
function sidePartitions(card){const labels={main:'主卡',extra:'额外',side:'Side'},value=card.partitions;const keys=Array.isArray(value)?value:Object.keys(value||{});return keys.map(p=>labels[p]||p).join('／')||'—'}
function sideCardsText(cards){return (cards||[]).map(c=>`${sideName(c.code,c.name)} ×${c.copies}${sidePartitions(c)!=='—'?'（'+sidePartitions(c)+'）':''}`).join('、')}
function sideUsage(card){
  const use=card.usage||{},wr=use.win_rate||{};
  if(!use.used_known_games){
    const hint=use.side_in_games?
      (use.used_games?`换入${use.side_in_games}局、使用${use.used_games}局，但有效胜负样本不足`:
        `换入${use.side_in_games}局，使用/胜负未知`):
      '无换入且使用的有效小局';
    return '无有效样本（'+hint+'）';
  }
  return `${wr.percentage===null?'—':wr.percentage.toFixed(1)+'%'}（${wr.wins}胜/${wr.losses}负/${wr.draws}平；有效使用${use.used_known_games}局/${use.used_games}局）`;
}
function sideInvestment(card){
  const inv=card.investment||{};
  if(!inv.denominator)return '无有效样本';
  return `${inv.sessions} / ${inv.denominator}（${inv.percentage===null?'—':inv.percentage.toFixed(1)+'%'}）`;
}
function sideTable(target,rows,side,direction){
  target.replaceChildren();
  const valid=side.sample.valid_sessions||0;
  const withUse=direction==='换入';
  const columns=withUse?['卡名','卡号','分区','投入场次 / 有效场次（投入率）','单场投入张数分布','支持玩家数','使用后胜率（胜/负/平；使用/分母）','最近出现']
    :['卡名','卡号','分区','出现场次 / 有效场次','单场张数分布','支持玩家数','最近出现'];
  if(!rows.length){
    const body=el('tbody'),tr=el('tr'),td=el('td',valid?'没有达到有效分母的'+direction+'记录；未知或证据不足的场次见下方，不当作没有换牌。':'没有可用于计算换入/换出的有效场次；未知不等于没有换牌。','note');
    td.colSpan=columns.length;tr.append(td);body.append(tr);target.append(body);return;
  }
  const head=el('thead'),hr=el('tr');
  for(const text of columns)hr.append(el('th',text));
  head.append(hr);target.append(head);
  const body=el('tbody');
  for(const card of rows){
    const tr=el('tr');
    tr.append(el('td',sideName(card.code,card.name)),el('td','#'+card.code),el('td',sidePartitions(card)));
    if(withUse){
      const copies=card.investment&&card.investment.copies?Object.entries(card.investment.copies).map(([n,c])=>`${n}张×${c}场`).join('／'):sideCopies(card);
      tr.append(el('td',sideInvestment(card)),el('td',copies||'—'),
        el('td',`${card.investment?card.investment.players:card.players} 名`),
        el('td',sideUsage(card)));
    }else{
      tr.append(el('td',`${card.sessions} / ${valid}（${sidePct(card)}）`),el('td',sideCopies(card)),
        el('td',`${card.players} 名`));
    }
    tr.append(el('td',card.last_seen?SS.time(card.last_seen):'—'));
    body.append(tr);
  }
  target.append(body);
}
function sideSample(side){
  const s=side.sample,box=$('sideSample');box.replaceChildren();
  const cell=(title,text)=>box.append(el('div',title+'：'+text));
  cell('统计单位',s.unit==='player_side'?'玩家-场次（内战双方各自作为我方）':'场（同一场多局取每卡最大新增，不累加）');
  cell('样本',`匹配 ${s.sessions_matched} 场／换边证据有效 ${s.valid_sessions} 场／有明确胜负 ${s.valid_outcome_sessions||0} 场／场结果未知 ${s.outcome_unknown_sessions||0} 场`);
  cell('有换边证据',`${s.sessions_with_changes} 场；明确无变化 ${s.sessions_without_changes} 场`);
  cell('换边局',`${s.later_games} 个（场次局数：${Object.entries(s.game_counts||{}).map(([n,c])=>n+'局×'+c).join('、')||'—'}）`);
  if(s.sessions_with_gaps)cell('缺少中间局次',`${s.sessions_with_gaps} 场（只按已观测局汇总，缺局不代表没换牌）`);
  cell('先后攻',`先攻 ${s.seat.first}／后攻 ${s.seat.second}／未知 ${s.seat.unknown}`);
  cell('玩家与对手',`${s.players} 名玩家／${s.opponents} 名对手`);
  const reasons=Object.entries(s.excluded||{}).map(([key,count])=>`${SIDE_STATUS[key]||key} ${count}`).join('、');
  cell('未计入分母',reasons||'无');
  if(s.unassociated_views)cell('方向匹配但无法归入场次',`${s.unassociated_views} 个有效视角（不按时间补猜）`);
  const opp=(s.by_opponent_deck||[]).slice(0,10).map(item=>`${item.deck} ${item.sessions}`).join('、');
  if(opp)cell('对手卡组（匹配场次）',opp);
  const players=(s.by_player||[]).slice(0,10).map(item=>`${item.player} ${item.valid_sessions}/${item.sessions}`).join('、');
  if(players)cell('玩家（有效/匹配场次）',players);
  if(s.date_range)cell('时间范围',`${SS.time(s.date_range[0])} — ${SS.time(s.date_range[1])}`);
}
function renderSideAlgorithm(side){
  const box=$('sideAlgorithm'),a=side.algorithm;box.replaceChildren();
  box.append(el('h3','既有算法推断（Side 候选，不是当场确定事实）'));
  if(!a.available){
    const reason=a.reason==='disabled'?'本入口未计算算法推断。':(SIDE_REASON[a.reason]||'没有可用于推断的唯一可靠首局');
    box.append(el('p',reason+'；不生成候选，也不当作没有 Side。','note'));
    for(const note of a.notes||[])box.append(el('p',note,'note'));
    return;
  }
  const reasons=Object.entries(a.reason_counts||{}).map(([key,count])=>`${SIDE_REASON[key]||key} ${count}`).join('、');
  box.append(el('p',`按玩家分别调用既有 infer_side；覆盖 ${a.players_analyzed}/${a.players_total} 名玩家${a.players_skipped?'（其余未纳入汇总）':''}。${reasons?'原因：'+reasons+'。':''}`,'note'));
  const chips=el('div');
  for(const card of a.cards||[]){
    const chip=el('span',`${sideName(card.code,card.name)} ×${card.copies_max||1} · ${card.players}名候选 · 本对阵观测 ${card.observed_players}名`,'side-chip');
    if(card.latent_players)chip.textContent+=` · latent ${card.latent_players}名`;
    chips.append(chip);
  }
  for(const card of a.latent_cards||[])chips.append(el('span',`${sideName(card.code,card.name)} ×${card.copies_max||1} · latent ${card.players}名`,'side-chip latent'));
  for(const card of a.observed_only||[])chips.append(el('span',`${sideName(card.code,card.name)} · 有逐场换入、未进候选`,'side-chip latent'));
  if(!chips.childNodes.length)box.append(el('p','没有生成候选；这不等于没有 Side。','note'));else box.append(chips);
  for(const note of a.notes||[])box.append(el('p',note,'note'));
}
function renderSide(result){
  const side=result.sides[0],s=side.sample;
  $('sideContent').hidden=false;
  $('sideStatus').textContent=`已读取：有效 ${s.valid_sessions} 场／匹配 ${s.sessions_matched} 场`;
  const scopeText=sideScope==='build'&&data&&data.selected?'当前构筑（主卡相同）':'该玩家全部有效小局';
  $('sideMeta').textContent=`Side 范围：${scopeText} · ${side.filters.first==='first'?'该玩家先攻':side.filters.first==='second'?'该玩家后攻':'全部先后攻'} · 统计单位：${s.unit==='player_side'?'玩家-场次（内战）':'场'} · 换边证据有效 ${s.valid_sessions} · 有明确胜负 ${s.valid_outcome_sessions||0} · 场结果未知 ${s.outcome_unknown_sessions||0} · 未计入 ${Object.values(s.excluded||{}).reduce((a,b)=>a+b,0)} 场 · 换入 ${side.observed.added.length} 种／换出 ${side.observed.removed.length} 种`;
  sideTable($('sideAdded'),side.observed.added,side,'换入');
  sideTable($('sideRemoved'),side.observed.removed,side,'换出');
  $('sideAddedNote').textContent='投入率按场：分母为至少一个“首局可靠、换边比较完整、该小局胜负明确”的有效换边小局的场次；分母包含明确没有换入该卡的场次，不能用同场另一局替未知局取得资格。使用后胜率只按“该卡在同一有效换边小局换入且实际使用”的小局计算，平局进分母；未知不计入正式指标。若首局已有该卡、换边只增加张数，used=1 只能证明该卡号被使用，不能证明用到新换入的那一张；不做实体卡追踪或推断。';
  $('sideRemovedNote').textContent='换出同样只认逐局卡表差异；未知不当作没有换出。支持玩家数与场次频次分开。';
  sideSample(side);
  const directBox=$('sideDirect');directBox.replaceChildren();
  const direct=side.observed.direct_side;
  if(direct&&direct.sessions){
    const block=el('div',undefined,'side-algorithm');
    block.append(el('h3','直接记录的 Side（没有换入/换出方向）'));
    block.append(el('p',`${direct.sessions} 个场次有直接 Side 卡表记录；它只能说明当时 Side 内容，不能当成换入/换出证据。`,'note'));
    const chips=el('div');
    for(const card of direct.cards||[])chips.append(el('span',`${sideName(card.code,card.name)} ×${card.max_copies}`,'side-chip'));
    block.append(chips);directBox.append(block);
  }
  const unknown=side.observed.unknown_outcome||{};
  if(unknown.cards&&unknown.cards.length){
    const block=el('div',undefined,'side-algorithm');
    block.append(el('h3','胜负未知、未计入正式指标的换入证据'));
    block.append(el('p',`${unknown.sessions} 个场次的换边小局胜负未知；下列卡片只作单列说明，不进入投入率、支持玩家数或使用后胜率。`,'note'));
    const chips=el('div');
    for(const card of unknown.cards)chips.append(el('span',`${sideName(card.code,card.name)} ×${card.max_copies}（${card.sessions}场）`,'side-chip latent'));
    block.append(chips);directBox.append(block);
  }
  const sessions=$('sideSessions');sessions.replaceChildren();
  if(side.sessions.length){
    const head=el('thead'),hr=el('tr');
    for(const text of ['时间','对手','对手卡组','局数','状态','换入','换出','源'])hr.append(el('th',text));
    head.append(hr);sessions.append(head);
    const body=el('tbody');
    for(const item of side.sessions){
      const tr=el('tr'),status=el('td',item.status_label||SIDE_STATUS[item.status]||item.status,'note '+(item.status==='observed'?'side-status-observed':'side-status-unknown'));
      const added=item.added.length?sideCardsText(item.added):(item.status==='observed'?'明确无变化':'未知');
      const removed=item.removed.length?sideCardsText(item.removed):(item.status==='observed'?'明确无变化':'未知');
      const suffix=item.status==='observed'?'':'（未计入分母）';
      if((item.unknown_outcome_games||[]).length)status.textContent+=`（第${item.unknown_outcome_games.join('、')}局胜负未知）`;
      const source=el('td');
      const firstGame=(item.games||[])[0];
      if(firstGame&&firstGame.digest){const link=el('a','录像');link.href='/statistics/sample/'+firstGame.digest+'?player='+encodeURIComponent(item.player);source.append(link)}
      tr.append(el('td',SS.time(item.recorded_at)),el('td',item.opponent||'未知'),el('td',item.opponent_deck||'未识别'),
        el('td',`${item.game_count||'?'}局${item.later_games!==undefined?'／换边局'+item.later_games:''}`),status,
        el('td',added+suffix),el('td',removed+suffix),source);
      body.append(tr);
    }
    sessions.append(body);
  }else sessions.append(el('p','没有匹配该方向的场次。','note'));
  renderSideAlgorithm(side);
  $('sideNotes').textContent=[...(result.notes||[]),...(side.notes||[])].join(' ');
}
function recentTable(id,cards,matches){
  const table=$(id);table.replaceChildren();
  const head=el('thead'),hr=el('tr');
  for(const text of ['卡名','分区','出现场次','单场张数'])hr.append(el('th',text));
  head.append(hr);table.append(head);
  const body=el('tbody');
  for(const card of cards||[]){
    const copies=card.min_copies===card.max_copies?`×${card.max_copies}`:`×${card.min_copies}–${card.max_copies}`;
    const tr=el('tr');
    tr.append(el('td',sideName(card.code,card.name)),el('td',card.partition==='main'?'主卡':'额外'),
      el('td',`${card.sessions}/${matches}场`),el('td',copies));
    body.append(tr);
  }
  if(!(cards||[]).length){
    const tr=el('tr'),td=el('td','无明确换入/换出','note');td.colSpan=4;tr.append(td);body.append(tr);
  }
  table.append(body);
}
function recentSessionTable(sessions){
  const table=$('recentSessions');table.replaceChildren();
  const head=el('thead'),hr=el('tr');
  for(const text of ['时间','对手','局数','换边局先后攻','换入','换出','源'])hr.append(el('th',text));
  head.append(hr);table.append(head);
  const body=el('tbody');
  for(const item of sessions||[]){
    const seats=[...new Set((item.side_seats||[]).map(value=>value==='first'?'先攻':'后攻'))];
    const first=seats.length?seats.join('＋'):(item.first_player===1?'先攻':item.first_player===0?'后攻':'未知');
    const noEvidence=!(item.side_games||0);
    const added=noEvidence?'无换边局':(item.added||[]).map(card=>`${sideName(card.code,card.name)}×${card.copies}`).join('、')||'明确无变化';
    const removed=noEvidence?'无换边局':(item.removed||[]).map(card=>`${sideName(card.code,card.name)}×${card.copies}`).join('、')||'明确无变化';
    const tr=el('tr'),source=el('td');
    if(item.digest){const link=el('a','录像');link.href='/statistics/sample/'+item.digest+'?player='+encodeURIComponent(player);source.append(link)}
    tr.append(el('td',SS.time(item.recorded_at)),el('td',item.opponent||'未知'),
      el('td',`${item.game_count||'?'}局／换边局${item.side_games||0}`),el('td',first),
      el('td',added),el('td',removed),source);
    body.append(tr);
  }
  if(!(sessions||[]).length){
    const tr=el('tr'),td=el('td','最近5场没有完整可比较的换边局','note');td.colSpan=7;tr.append(td);body.append(tr);
  }
  table.append(body);
}
function fillRecentOptions(result){
  const select=$('sideOpponent'),current=select.value;
  select.replaceChildren();
  for(const item of result.opponents||[]){
    const option=el('option',`${item.deck}（${item.matches}场）`);option.value=item.deck;select.append(option);
  }
  if([...select.options].some(option=>option.value===current))select.value=current;
}
function renderRecent(){
  const box=$('sideRecent'),opponents=(recentData&&recentData.opponents)||[];
  $('sideContent').hidden=true;box.hidden=false;
  if(!opponents.length){
    $('sideRecentMeta').textContent='当前构筑最近14天没有足够的对手卡组数据。';
    for(const id of ['recentFirstIn','recentFirstOut','recentSecondIn','recentSecondOut'])$(id).replaceChildren();
    recentSessionTable([]);$('sideRecentNotes').textContent=(recentData&&recentData.notes||[]).join(' ');
    return;
  }
  let item=opponents.find(value=>value.deck===$('sideOpponent').value)||opponents[0];
  if($('sideOpponent').value!==item.deck)$('sideOpponent').value=item.deck;
  const low=item.sample_low?'（样本少：完整换边局少于3个）':'';
  $('sideRecentMeta').textContent=`${item.deck}：该对手共 ${item.matches} 场；本次取最近 ${item.recent_matches} 场，${item.side_games} 个Side局${low}。`;
  recentTable('recentFirstIn',item.first.added,item.recent_matches);
  recentTable('recentFirstOut',item.first.removed,item.recent_matches);
  recentTable('recentSecondIn',item.second.added,item.recent_matches);
  recentTable('recentSecondOut',item.second.removed,item.recent_matches);
  const titles=[['recentFirstIn',`先攻（${item.first.side_games}局） · 换入`],
                ['recentFirstOut',`先攻（${item.first.side_games}局） · 换出`],
                ['recentSecondIn',`后攻（${item.second.side_games}局） · 换入`],
                ['recentSecondOut',`后攻（${item.second.side_games}局） · 换出`]];
  for(const [id,text] of titles){const table=$(id),section=table.closest('section');if(section)section.querySelector('h3').textContent=text}
  recentSessionTable(item.sessions);
  $('sideRecentNotes').textContent=(recentData.notes||[]).join(' ');
}
async function loadRecentSide(force=false){
  if(!player){$('sideStatus').textContent='请从玩家排行或小局来源打开构筑。';return}
  const id=++sideRequest;
  if(!recentData||force){
    $('sideStatus').textContent='正在读取换备（最近5场）…';
    const q=new URLSearchParams({player,top:'6',recent:'5'});
    if(data&&data.selected)q.set('digest',data.selected.digest);
    try{
      const result=await SS.api('side-recent?'+q);
      if(id!==sideRequest)return;
      recentData=result;
    }catch(error){
      if(id!==sideRequest)return;
      $('sideStatus').textContent='换备读取失败：'+cleanError(error);
      return;
    }
  }
  if(!recentData){$('sideStatus').textContent='没有找到当前构筑。';return}
  fillRecentOptions(recentData);
  if(!$('sideOpponent').value&&recentData.opponents.length)$('sideOpponent').value=recentData.opponents[0].deck;
  renderRecent();
  $('sideStatus').textContent='已读取最近5场换备';
}
function loadSideView(){
  sideWindow=$('sideWindow').value;
  if(sideWindow==='recent'){
    $('sideFirst').hidden=true;$('sideFirst').disabled=true;
    $('sideMeta').textContent='最近5场口径：当前构筑，按对手场次取前6。';
    $('sideContent').hidden=true;$('sideRecent').hidden=false;
    loadRecentSide(false);
  }else{
    $('sideFirst').hidden=false;$('sideFirst').disabled=false;
    $('sideRecent').hidden=true;$('sideContent').hidden=false;
    const select=$('sideOpponent');select.replaceChildren();
    const option=el('option','全部对手');option.value='';select.append(option);
    sideOptionsDone=false;
    loadSide('');
  }
}
function sideOptions(side){
  const select=$('sideOpponent');if(sideOptionsDone)return;
  for(const item of side.sample.by_opponent_deck||[]){
    if(!item.deck||item.deck==='未识别')continue;
    if([...select.options].some(option=>option.value===item.deck))continue;
    const option=el('option',`${item.deck}（${item.sessions}场）`);option.value=item.deck;select.append(option);
  }
  sideOptionsDone=true;
}
async function loadSide(opponent){
  if(!player){$('sideStatus').textContent='请从玩家排行或小局来源打开构筑。';return}
  const id=++sideRequest,q=new URLSearchParams({mode:'player',player});
  sideScope=$('scope').value;
  q.set('scope',sideScope);
  if(data&&data.selected)q.set('digest',data.selected.digest);
  if(opponent)q.set('opponent_deck',opponent);
  const first=$('sideFirst')?$('sideFirst').value:'all';
  if(first!=='all')q.set('first',first);
  const seat=first==='first'?'（该玩家先攻）':first==='second'?'（该玩家后攻）':'（全部先后攻）';
  $('sideStatus').textContent=(opponent?`正在读取对手「${opponent}」`:'正在读取全部对手')+seat+'的换边证据…';
  try{
    const result=await SS.api('side-analysis?'+q);
    if(id!==sideRequest)return;
    if(!opponent)sideOptions(result.sides[0]);
    renderSide(result);
  }catch(error){
    if(id!==sideRequest)return;
    $('sideStatus').textContent='Side 分析读取失败：'+cleanError(error)+' 可点“重算”重试。';
  }
}
$('sideOpponent').onchange=()=>{if(sideWindow==='recent')renderRecent();else loadSide($('sideOpponent').value)};
$('sideFirst').onchange=()=>{if(sideWindow!=='recent')loadSide($('sideOpponent').value)};
$('sideReload').onclick=()=>{if(sideWindow==='recent'){recentData=null;loadRecentSide(true)}else loadSide($('sideOpponent').value)};
$('sideWindow').onchange=()=>{recentData=null;sideOptionsDone=false;loadSideView()};
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
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),120000);
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

