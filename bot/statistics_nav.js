(()=>{
  const nav=document.querySelector('body>.main-nav');
  if(!nav)return;
  const marker=location.pathname.indexOf('/statistics');
  if(marker<0)return;
  const prefix=location.pathname.slice(0,marker);
  const p=location.pathname.slice(marker);
  const links=[['/statistics','玩家排名'],['/statistics/cards-report','卡片统计'],
    ['/statistics/cards-extra','额外卡'],['/statistics/matrix','卡组胜率'],
    ['/statistics/recordings','录像筛选'],['/statistics/deck-naming','卡组命名'],
    ['/statistics/devices','设备与观战'],['/statistics/small-world','小世界']];
  let active;
  if(p.includes('/small-world'))active='/statistics/small-world';
  else if(p.includes('/devices'))active='/statistics/devices';
  else if(p.includes('deck-naming'))active='/statistics/deck-naming';
  else if(p.includes('cards-extra'))active='/statistics/cards-extra';
  else if(p.includes('card')||p.includes('investment'))active='/statistics/cards-report';
  else if(p.includes('matrix'))active='/statistics/matrix';
  else if(p.includes('recording'))active='/statistics/recordings';
  else active='/statistics';
  nav.setAttribute('aria-label','主要功能');
  nav.replaceChildren(...links.map(([href,label])=>{
    const a=document.createElement('a');a.href=prefix+href;a.textContent=label;
    if(href===active)a.setAttribute('aria-current','page');
    if(href==='/statistics/small-world')a.dataset.smallWorld='1';
    return a;
  }));
})();
