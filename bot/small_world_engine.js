/* Five-property graph and bridge scoring adapted from KennethJAllen/ygo-small-world.
 * Copyright (c) 2023 Kenneth Allen, MIT. See licenses/ygo-small-world-MIT.txt.
 * Bit sets replace global adjacency matrices; YDK contents stay in the browser.
 */
(function(root){
  'use strict';
  const labels=['种族','属性','等级','攻击力','守备力'];
  const key=c=>JSON.stringify([c.name.normalize('NFKC').replace(/\s/g,'').toLowerCase(),...c.props]);
  function unpack(rows){
    if(!Array.isArray(rows)||!rows.length||rows.length>50000)throw Error('卡库内容无效。');
    const index=new Map();
    for(const row of rows){
      if(!Array.isArray(row)||row.length!==8||!Number.isInteger(row[0])||row[0]<=0||row[0]>0xffffffff||
        typeof row[1]!=='string'||!row[1].trim()||!Number.isInteger(row[2])||
        row.slice(3).some(v=>v!==null&&(!Number.isSafeInteger(v)||v<0)))throw Error('卡库格式异常，请重新加载。');
      index.set(row[0],{id:row[0],name:row[1],kind:row[2],props:row.slice(3),
        main:!!(row[2]&1)&&!(row[2]&(0x40|0x2000|0x800000|0x4000000|0x4000))});
    }
    return index;
  }
  function parseYdk(text){
    if(typeof text!=='string'||text.length>65536)throw Error('YDK 文件过大，最多 64 KB。');
    const parts={main:[],extra:[],side:[]};let section=null;const seen=new Set();
    for(const raw of text.replace(/^\uFEFF/,'').split(/\r?\n/)){
      const line=raw.trim();if(!line)continue;
      const mark={'#main':'main','#extra':'extra','!side':'side'}[line.toLowerCase()];
      if(mark){if(seen.has(mark))throw Error('YDK 分区重复，请重新导出卡组。');seen.add(mark);section=mark;continue;}
      if(line.startsWith('#'))continue;
      if(!section||!/^\d{1,10}$/.test(line)||Number(line)<=0||Number(line)>0xffffffff)throw Error('YDK 中有无效卡号或缺少 #main 分区。');
      parts[section].push(Number(line));
      if(parts.main.length>60||parts.extra.length>15||parts.side.length>15)throw Error('卡组超过分区上限：主卡 60、额外 15、Side 15。');
    }
    if(!seen.has('main')||!parts.main.length)throw Error('YDK 没有主卡组，请选择有效的 .ydk 文件。');
    return parts;
  }
  function shared(a,b){return labels.filter((_,i)=>a.props[i]!==null&&b.props[i]!==null&&a.props[i]===b.props[i]);}
  const linked=(a,b)=>shared(a,b).length===1;
  function makeDeck(parts,index){
    const unique=new Map(),unknown=[],ignored=[];
    for(const id of parts.main){
      const c=index.get(id);if(!c){unknown.push(id);continue;}
      if(!c.main){ignored.push(c);continue;}
      const identity=key(c),previous=unique.get(identity);
      if(previous)previous.copies++;else unique.set(identity,{...c,copies:1});
    }
    return {cards:[...unique.values()],unknown,ignored,total:parts.main.length,extra:parts.extra.length,side:parts.side.length};
  }
  const bit=i=>1n<<BigInt(i);
  function count(mask){let result=0;while(mask){mask&=mask-1n;result++;}return result;}
  function graph(cards){
    const adjacency=cards.map(a=>cards.reduce((mask,b,j)=>linked(a,b)?mask|bit(j):mask,0n));
    const reachable=cards.map((_,i)=>adjacency.reduce((mask,row,k)=>(adjacency[i]&bit(k))?mask|row:mask,0n));
    return {cards,adjacency,reachable,score:reachable.reduce((sum,row)=>sum+count(row),0)/(cards.length**2||1)};
  }
  function candidateScore(g,c){
    const n=g.cards.length;let x=0n;
    g.cards.forEach((d,i)=>{if(linked(c,d))x|=bit(i);});
    if(!x)return null;
    let reachable=0,cross=0;
    for(let i=0;i<n;i++){
      reachable+=count(g.reachable[i]|((x&bit(i))?x:0n));
      if(g.adjacency[i]&x)cross++;
    }
    return {card:c,connections:count(x),score:(reachable+2*cross+1)/((n+1)**2)};
  }
  function pool(index,deck,target=null){
    const existing=new Set(deck.map(key)),seen=new Set(),result=[];
    for(const c of index.values()){
      const identity=key(c);
      if(!c.main||existing.has(identity)||seen.has(identity)||(target&&!linked(c,target)))continue;
      seen.add(identity);result.push(c);
    }
    return result;
  }
  const sort=(a,b)=>b.score-a.score||b.connections-a.connections||a.card.id-b.card.id;
  function bridges(cards,start,target){
    return cards.filter(c=>linked(start,c)&&linked(c,target)).map(c=>({card:c,first:shared(start,c)[0],second:shared(c,target)[0]}));
  }
  const api={labels,key,unpack,parseYdk,shared,linked,makeDeck,graph,candidateScore,pool,sort,bridges};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.SmallWorldEngine=api;
})(globalThis);
