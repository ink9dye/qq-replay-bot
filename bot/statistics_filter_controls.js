window.FilterControls={
 values(id){const s=document.getElementById(id);return s.dataset.none==='1'?['"__no_selection__"']:Array.from(s.selectedOptions).map(o=>o.value).filter(Boolean)},
 enhance(ids,changed){for(const id of ids){const s=document.getElementById(id);s.multiple=true;s.size=4;const tools=document.createElement('div'),summary=document.createElement('small');tools.style.cssText='display:flex;gap:6px;flex-wrap:wrap;margin:6px 0';summary.style.display='block';const update=()=>{const values=Array.from(s.selectedOptions).filter(o=>o.value);summary.textContent=values.length?'已选：'+values.map(o=>o.textContent).join('、'):'不限制';};{const b=document.createElement('button');b.type='button';b.textContent='清空';b.className='secondary';b.onclick=()=>{for(const o of s.options)o.selected=false;s.dataset.none='0';update();changed()};tools.append(b)}s.after(tools,summary);s.addEventListener('change',()=>{s.dataset.none='0';update()});update()}}
};

// Card choices toggle independently on a plain click; no Ctrl/Command required.
// allowedParts() 返回当前模式允许的构筑分区：抽到/未抽到只对主卡组有意义，所以额外与Side不提供选择。
FilterControls.cardPicker=function(id,searchId,allowedParts){
 const select=document.getElementById(id),search=document.getElementById(searchId);
 select.multiple=true;select.hidden=true;select.style.setProperty('display','none','important');
 const choices=document.createElement('div'),chips=document.createElement('div'),tools=document.createElement('div'),hint=document.createElement('small');
 choices.className='card-choices';chips.className='card-chips';chips.setAttribute('aria-label','已选卡片');
 choices.style.cssText='max-height:180px;overflow:auto;display:flex;flex-wrap:wrap;gap:4px';chips.style.cssText='display:flex;flex-wrap:wrap;gap:4px;margin:8px 0';
 let visible=[];
 const update=()=>{
  const selected=Array.from(select.selectedOptions);
  chips.replaceChildren();
  for(const option of selected){const chip=document.createElement('span'),remove=document.createElement('button');chip.append(document.createTextNode(option.textContent+' '));remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除'+option.textContent);remove.onclick=()=>{option.selected=false;update()};chip.append(remove);chips.append(chip)}
  hint.textContent=selected.length?'已选'+selected.length+'张卡；点击×移除':'不限制卡片';
  const parts=allowedParts?allowedParts():null,term=search.value.trim().toLowerCase();
  visible=Array.from(select.options).filter(o=>{
   if(parts&&!parts.includes(o.dataset.part||'main'))return false;
   return !term||o.textContent.toLowerCase().includes(term)});
  choices.replaceChildren();
  for(const option of visible.slice(0,30)){const button=document.createElement('button');button.type='button';button.textContent=option.textContent;button.setAttribute('aria-pressed',String(option.selected));button.onclick=()=>{option.selected=!option.selected;update()};choices.append(button)}
  if(visible.length>30){const more=document.createElement('small');more.textContent='显示前30项，请输入卡名或卡号缩小范围';choices.append(more)}
  else if(!visible.length){const none=document.createElement('small');none.textContent='当前条件下没有可选的卡';choices.append(none)}
 };
 {const button=document.createElement('button');button.type='button';button.textContent='清空';button.className='secondary';button.onclick=()=>{for(const o of select.options)o.selected=false;update()};tools.append(button)}
 select.after(choices,chips,tools,hint);search.oninput=update;select.addEventListener('change',update);update();return update;
};
