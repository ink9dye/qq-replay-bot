(()=>{
  'use strict';
  const E=SmallWorldEngine,$=id=>document.getElementById(id);
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const percent=x=>(x*100).toFixed(1)+'%',code=c=>String(c.id).padStart(8,'0');
  let catalog=null,loading=null,deck=null,g=null,ranking=[],shown=0,revision=0;
  const pause=()=>new Promise(resolve=>setTimeout(resolve,0));
  function error(message){$('error').textContent=message;$('error').hidden=false;}
  async function loadCatalog(){
    if(catalog)return catalog;
    if(!loading)loading=(async()=>{
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
      try{
        const response=await fetch('/statistics/api/small-world-cards',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
        if(!response.ok)throw Error(response.status===403||response.status===410?'统计链接已失效，请重新获取统计链接。':'卡库暂时无法加载，请稍后重试。');
        const data=await response.json();catalog=E.unpack(data.cards);return catalog;
      }finally{clearTimeout(timer);loading=null;}
    })();
    return loading;
  }
  function fillSelect(select,cards,includeAll=false){
    select.replaceChildren();if(includeAll)select.append(new Option('不限目标，优先整体覆盖',''));
    for(const card of cards)select.append(new Option(card.name+' · '+card.copies+'张',String(card.id)));
  }
  function renderPaths(){
    const host=$('paths');host.replaceChildren();
    const start=deck.cards.find(c=>String(c.id)===$('start').value),target=deck.cards.find(c=>String(c.id)===$('target').value);
    if(!start||!target)return;
    const paths=E.bridges(deck.cards,start,target);
    if(!paths.length){host.append(el('p','这两张怪兽之间，牌组中还没有可用的中转卡。试试下面的中转卡推荐。','empty'));return;}
    for(const row of paths){
      const node=el('div',undefined,'path'),name=el('strong',row.card.name);
      name.append(el('small','  '+code(row.card)));node.append(name,
        el('span','手牌→中转：仅'+row.first+'相同','property'),el('span','中转→目标：仅'+row.second+'相同','property'));host.append(node);
    }
    if(E.key(start)===E.key(target))host.append(el('p','起点与目标同名，实际检索时牌组中还需要另一张目标卡。','note'));
  }
  function candidateDetail(row){
    const host=$('candidateDetail');host.replaceChildren(el('h3',row.card.name+' · '+code(row.card)));
    const connections=deck.cards.filter(c=>E.linked(c,row.card));
    host.append(el('p','可直接连接的牌组怪兽（这'+connections.length+'种怪兽可经它彼此检索）：'));
    const list=el('ul');
    for(const c of connections)list.append(el('li',c.name+' ↔ '+row.card.name+'：仅'+E.shared(c,row.card)[0]+'相同'));
    host.append(list,el('p','加入后的路径覆盖：'+percent(row.score)+'。中转卡需实际加入主卡组，牌组张数与禁限状态请自行核对。','note'));
    host.hidden=false;host.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function showMore(){
    const body=$('recommendations'),next=Math.min(shown+20,ranking.length);
    for(const row of ranking.slice(shown,next)){
      const tr=el('tr'),name=el('td',row.card.name),action=el('td'),button=el('button','连接详情','secondary');
      name.append(el('small',code(row.card)));button.type='button';button.addEventListener('click',()=>candidateDetail(row));action.append(button);
      tr.append(name,el('td',row.connections+'种怪兽'),el('td',percent(row.score)),action);body.append(tr);
    }
    shown=next;$('more').hidden=shown>=ranking.length;
  }
  async function recommend(){
    const myRevision=++revision;
    $('error').hidden=true;$('rerank').disabled=true;$('recommendations').replaceChildren();$('more').hidden=true;
    $('candidateDetail').hidden=true;$('recommendCount').textContent='计算中';$('status').textContent='正在寻找更合适的中转卡…';
    try{
      const target=deck.cards.find(c=>String(c.id)===$('focus').value)||null;
      const candidates=E.pool(catalog,deck.cards,target),result=[];
      for(let i=0;i<candidates.length;i++){
        const row=E.candidateScore(g,candidates[i]);if(row)result.push(row);
        if(i%128===0){await pause();if(myRevision!==revision)return;}
      }
      if(myRevision!==revision)return;
      ranking=result.sort(E.sort);shown=0;
      $('recommendCount').textContent=ranking.length+'张候选';
      if(!ranking.length){const tr=el('tr'),td=el('td','没有符合条件的新中转卡，试试不限检索目标。','empty');td.colSpan=4;tr.append(td);$('recommendations').append(tr);}
      else showMore();
      $('status').textContent='分析完成。可以切换起点、目标或重点检索怪兽继续查看。';
    }catch(exc){if(myRevision===revision)error('推荐计算失败：'+exc.message);}
    finally{if(myRevision===revision)$('rerank').disabled=false;}
  }
  async function analyze(text,name){
    const myRevision=++revision;$('error').hidden=true;$('results').hidden=true;$('status').textContent='正在读取卡组和卡库…';
    try{
      const parts=E.parseYdk(text);await loadCatalog();if(myRevision!==revision)return;
      const next=E.makeDeck(parts,catalog);
      if(!next.cards.length)throw Error('主卡组中没有可分析的怪兽。'+(next.unknown.length?'其中有卡号未被当前卡库收录。':''));
      deck=next;g=E.graph(deck.cards);
      $('mainCount').textContent=deck.total+'张';$('monsterCount').textContent=deck.cards.length+'种';$('coverage').textContent=percent(g.score);
      $('deckNote').textContent=(name?name+' · ':'')+'分析'+deck.cards.reduce((n,c)=>n+c.copies,0)+'张主卡怪兽；忽略'+deck.ignored.length+'张魔法/陷阱等卡片，以及'+deck.extra+'张额外、'+deck.side+'张Side。'+(deck.total<40?'当前主卡不足40张，可作为局部卡表分析。':'');
      $('unknown').hidden=!deck.unknown.length;
      $('unknown').textContent='当前卡库未收录'+deck.unknown.length+'张主卡，已跳过：'+[...new Set(deck.unknown)].join('、')+'。以下结果可能不完整。';
      fillSelect($('start'),deck.cards);fillSelect($('target'),deck.cards);fillSelect($('focus'),deck.cards,true);
      $('target').selectedIndex=deck.cards.length-1;renderPaths();$('results').hidden=false;await recommend();
    }catch(exc){if(myRevision!==revision)return;$('status').textContent='尚未生成分析结果。';error(exc.name==='AbortError'?'卡库加载超时，请稍后重试。':exc.message);}
  }
  $('analyze').addEventListener('click',()=>analyze($('ydkText').value,'粘贴的卡组'));
  $('ydkFile').addEventListener('change',async event=>{
    const file=event.target.files[0];if(!file)return;
    const fileRevision=++revision;
    if(!/\.ydk$/i.test(file.name)||file.size>65536){++revision;$('results').hidden=true;error('请选择不超过64 KB的 .ydk 文件。');return;}
    try{const text=await file.text();if(fileRevision!==revision)return;await analyze(text,file.name);}catch{error('无法读取这个文件，请重新选择。');}
    finally{event.target.value='';}
  });
  $('demo').addEventListener('click',()=>{
    $('ydkText').value='#created by Small World example\n#main\n14558127\n97268402\n27204311\n#extra\n!side\n';
    analyze($('ydkText').value,'示例卡表');
  });
  $('start').addEventListener('change',renderPaths);$('target').addEventListener('change',renderPaths);
  $('rerank').addEventListener('click',recommend);$('focus').addEventListener('change',recommend);$('more').addEventListener('click',showMore);
})();
