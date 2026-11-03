(()=>{
  const p=location.pathname;
  const nav=document.querySelector('body>.main-nav');
  if(nav&&!nav.querySelector('a[href="/statistics/devices"]')){
    const a=document.createElement('a');a.href='/statistics/devices';a.textContent='设备与观战';nav.append(a);
  }
  let active;
  if(p.includes('/devices'))active='/statistics/devices';
  else if(p.includes('deck-naming'))active='/statistics/deck-naming';
  else if(p.includes('card')||p.includes('investment'))active='/statistics/card-table';
  else if(p.includes('matrix'))active='/statistics/matrix';
  else if(p.includes('recording'))active='/statistics/recordings';
  else active='/statistics';
  for(const a of document.querySelectorAll('body>.main-nav a')){
    a.removeAttribute('aria-current');
    if((a.getAttribute('href')||'').endsWith(active))a.setAttribute('aria-current','page');
  }
})();
