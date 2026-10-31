(()=>{'use strict';
const SS=window.StatisticsSamples,$=id=>document.getElementById(id),el=SS.el;
const player=new URLSearchParams(location.search).get('player');
let result=null,selected=-1;
const format=n=>Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const change=n=>(n>=0?'+':'')+format(n);
const outcome=g=>({win:'胜',loss:'负',draw:'平',unknown:'胜负未知'})[g.outcome];
const ns='http://www.w3.org/2000/svg';
function svg(tag,attrs){const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n}
function select(index){selected=index;const g=result.games[index],target=$('ptSelected');target.replaceChildren();
 target.append(el('strong',`第${g.number}场 · ${outcome(g)} · 对手 ${g.opponent}`),el('div',SS.time(g.recorded_at),'note'),el('div',`${format(g.before)} → ${format(g.after)}　${change(g.delta)} PT`,g.delta>=0?'pt-gain':'pt-loss'));
 for(const n of $('trend').querySelectorAll('circle[data-index]')){const active=Number(n.dataset.index)===index;n.setAttribute('r',active?'5':'2.5');n.setAttribute('fill',active?'#f59e0b':'#4e7fd2')}

}
function render(r){result=r;const container=$('trend');container.replaceChildren();
 if(!r.games.length){container.append(el('p','暂无竞技记录。','note'));return}
 const points=[r.initial_points,...r.games.map(g=>g.after)],width=650,height=220,pad={left:58,right:20,top:18,bottom:32},low=Math.min(...points),high=Math.max(...points),margin=Math.max(10,(high-low)*.12),min=low-margin,max=high+margin;
 const x=i=>pad.left+i*(width-pad.left-pad.right)/(points.length-1),y=v=>pad.top+(max-v)*(height-pad.top-pad.bottom)/(max-min);
 const chart=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':`${player}本月PT走势，点击曲线查看某场对手和积分变化`});
 for(let i=0;i<4;i++){const v=min+(max-min)*i/3,Y=y(v),text=svg('text',{x:pad.left-8,y:Y+4,'text-anchor':'end',fill:'#7a8aa0','font-size':11});text.textContent=v.toFixed(0);chart.append(svg('line',{x1:pad.left,y1:Y,x2:width-pad.right,y2:Y,stroke:'#e5ebf4'}),text)}
 chart.append(svg('polyline',{points:points.map((v,i)=>`${x(i)},${y(v)}`).join(' '),fill:'none',stroke:'#4e7fd2','stroke-width':2,'stroke-linejoin':'round'}));
 r.games.forEach((g,i)=>{const circle=svg('circle',{cx:x(i+1),cy:y(g.after),r:2.5,fill:'#4e7fd2','data-index':i,tabindex:0,role:'button','aria-label':`第${g.number}场，对手${g.opponent}，${outcome(g)}，${change(g.delta)}PT`});
  const title=svg('title',{});title.textContent=`${SS.time(g.recorded_at)} · ${g.opponent} · ${change(g.delta)}PT`;circle.append(title);circle.addEventListener('click',e=>{e.stopPropagation();select(i)});circle.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(i)}});chart.append(circle)});
 chart.addEventListener('click',e=>{const rect=chart.getBoundingClientRect(),px=(e.clientX-rect.left)*width/rect.width;select(Math.max(0,Math.min(r.games.length-1,Math.round((px-pad.left)*(points.length-1)/(width-pad.left-pad.right))-1)))});
 const start=svg('text',{x:pad.left,y:height-7,fill:'#7a8aa0','font-size':11});start.textContent='首场赛前';const end=svg('text',{x:width-pad.right,y:height-7,'text-anchor':'end',fill:'#7a8aa0','font-size':11});end.textContent=`第${r.count}场`;chart.append(start,end);container.append(chart);select(r.games.length-1);
}
async function load(){const button=$('retryTrend');button.hidden=true;$('trendStatus').textContent='正在自动读取本月竞技历史…';
 try{const r=await SS.api('pt-history?player='+encodeURIComponent(player));if(r.status!=='ok'){throw Error(r.message||'历史暂不可用。')}
  $('trendStatus').textContent=`${r.window_label} · ${r.count}场竞技对局 · 胜率${r.win_rate===null?'暂无':r.win_rate.toFixed(2)+'%'} · 点击曲线查看对手与积分变化`;
  $('winRate').textContent=r.win_rate===null?'暂无官方样本':r.win_rate.toFixed(1)+'%';$('officialRateMeta').textContent=`官方${r.window_label} · ${r.wins}胜 / ${r.count}场`;render(r);
 }catch(e){$('winRate').textContent='暂无官方数据';$('trend').replaceChildren(el('p','PT记录暂不可用','note'));$('trendStatus').textContent=e.message;button.hidden=false}finally{$('trend').setAttribute('aria-busy','false')}
}
$('retryTrend').onclick=load;if(player)load();
})();
