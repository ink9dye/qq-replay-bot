const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'../bot/',name),'utf8');
function setup(){
 const nodes=new Map(),observers=[];
 class E{
  constructor(tag='input',id=''){this.tagName=tag;this.id=id;this.children=[];this.dataset={};this.attrs={};this.listeners={};this.value='';this.hidden=false;this.style={setProperty(){}};this.classList={add(){}};this.selected=false;if(id)nodes.set(id,this)}
  append(...children){for(const c of children){if(c.parent)c.parent.children=c.parent.children.filter(x=>x!==c);this.children.push(c);c.parent=this}}
  before(n){if(this.parent){const p=this.parent,i=p.children.indexOf(this);p.children.splice(i,0,n);n.parent=p}}
  after(...children){if(this.parent)this.parent.append(...children)}
  replaceChildren(...children){this.children=[];this.append(...children)}
  setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]}removeAttribute(k){delete this.attrs[k]}
  addEventListener(k,fn,capture){(this.listeners[k]??=[]).push({fn,capture:!!capture})}
  dispatchEvent(event){event.preventDefault??=()=>{event.defaultPrevented=true};event.stopImmediatePropagation??=()=>{event.stopped=true};for(const x of [...(this.listeners[event.type]||[])].sort((a,b)=>b.capture-a.capture)){x.fn(event);if(event.stopped)break}return !event.defaultPrevented}
  focus(){document.activeElement=this;this.dispatchEvent({type:'focus'})}scrollIntoView(){}
  get options(){return this.children}get selectedOptions(){return this.children.filter(x=>x.selected)}
 }
 const document={readyState:'loading',activeElement:null,createElement:t=>new E(t),createTextNode:t=>Object.assign(new E('text'),{textContent:t}),getElementById:id=>nodes.get(id),querySelectorAll:()=>[],addEventListener(){}};
 const context={document,window:null,Event:class{constructor(type,options){this.type=type;Object.assign(this,options)}},MutationObserver:class{constructor(fn){observers.push(fn)}observe(){}},console,setTimeout,clearTimeout};context.window=context;
 vm.createContext(context);vm.runInContext(read('statistics_autocomplete.js'),context);
 const make=(id,tag='input')=>{const parent=new E('div'),n=new E(tag,id);parent.append(n);return n};
 return {context,document,make,E,observers,fire:(n,key,more={})=>n.dispatchEvent({type:key,...more})};
}
test('name/code filtering, card code detail and keyboard selection precede raw Enter',()=>{
 const s=setup(),input=s.make('q');let picked,raw=0;
 const control=s.context.StatisticsAutocomplete.attach(input,{items:()=>[{value:'14558127',label:'灰流丽',detail:'14558127'},{value:'2',label:'增殖的G'}],choose:x=>picked=x});
 input.addEventListener('keydown',()=>raw++);input.value='灰';input.focus();const list=input.parent.children[1];
 assert.equal(list.children.length,1);assert.equal(list.children[0].children[1].textContent,'14558127');
 s.fire(input,'keydown',{key:'Enter'});assert.equal(picked.value,'14558127');assert.equal(raw,0);assert.equal(list.hidden,true);
 input.value='1455';control.render();assert.equal(list.children.length,1);
});
test('IME Enter does not select and Escape closes; rendering is bounded at 40',()=>{
 const s=setup(),input=s.make('q');let picked=false;
 s.context.StatisticsAutocomplete.attach(input,{items:()=>Array.from({length:90},(_,i)=>({value:String(i),label:'卡'+i})),choose:()=>picked=true});input.focus();const list=input.parent.children[1];assert.equal(list.children.length,40);
 s.fire(input,'compositionstart');s.fire(input,'keydown',{key:'Enter',isComposing:true});assert.equal(picked,false);assert.equal(list.hidden,true);
 s.fire(input,'compositionend');s.fire(input,'keydown',{key:'ArrowUp'});assert.equal(list.children[39].attrs['aria-selected'],'true');s.fire(input,'keydown',{key:'Escape'});assert.equal(list.hidden,true);
});
test('late datalist population becomes visible and error explains empty candidates',()=>{
 const s=setup(),input=s.make('deck'),source=s.make('decks','datalist');input.setAttribute('list','decks');s.document.querySelectorAll=()=>[input];s.context.StatisticsAutocomplete.enhance();input.focus();const list=input.parent.children[1];input.dataset.candidateError='加载失败';s.observers[1]();assert.equal(list.children[0].textContent,'加载失败');
 const option=new s.E('option');option.value='雷盟';source.append(option);s.observers[0]();assert.equal(list.children[0].children[0].textContent,'雷盟');assert.equal(input.getAttribute('list'),undefined);
});
test('recording picker preserves main-only restriction, multiselect and chip removal',()=>{
 const s=setup(),input=s.make('search'),select=s.make('cards','select');
 for(const [code,part] of [['1','main'],['2','main'],['3','extra'],['4','side']]){const o=new s.E('option');o.value=code;o.textContent='卡'+code+' #'+code;o.dataset.part=part;select.append(o)}
 vm.runInContext(read('statistics_filter_controls.js'),s.context);let parts=['main'];const update=s.context.FilterControls.cardPicker('cards','search',()=>parts);input.focus();let list=input.parent.children[1];assert.equal(list.children.length,2);
 s.fire(list.children[0],'click');input.focus();s.fire(list.children[0],'click');assert.deepEqual(select.selectedOptions.map(o=>o.value),['1','2']);
 const chips=select.parent.children[1];chips.children[0].children[1].onclick();assert.deepEqual(select.selectedOptions.map(o=>o.value),['2']);parts=null;update();assert.equal(list.children.length,3);
});
function recordingSetup(fail){
 const s=setup(),ids=['deck','opponent','deck-list','opponent-list','cardSearch','opponentCardSearch','notCardSearch','opponentNotCardSearch','card','opponent_card','not_card','opponent_not_card','outcome','phase'];for(const id of ids)s.make(id);
 const old=new s.E('option');old.value='1';old.selected=true;s.document.getElementById('card').append(old);s.document.getElementById('outcome').value='loss';s.document.getElementById('phase').value='side_second';
 const calls=[];Object.assign(s.context,{$:s.document.getElementById,SS:{el:t=>new s.E(t),api:async name=>{calls.push(name);if(name===fail)throw Error('failure');return name==='groups'?{deck_groups:[{name:'雷盟'}]}:{rows:[{name:'灰流丽',code:1,base_partition:'main'},{name:'额外',code:2,base_partition:'extra'}]}}},updateOwn(){},updateOpponent(){},updateOwnNot(){},updateOpponentNot(){},load(){calls.push('load')}});
 const html=read('statistics_recordings.html'),source=html.slice(html.indexOf('let deckChoicesLoading='),html.indexOf('</script>',html.indexOf('let deckChoicesLoading=')));vm.runInContext(source,s.context);return {...s,calls};
}
test('failed deck candidates do not block cards or reset outcome/phase selections',async()=>{
 const s=recordingSetup('groups');await new Promise(r=>setImmediate(r));assert.equal(s.document.getElementById('card').options.length,2);assert.equal(s.document.getElementById('card').selectedOptions[0].value,1);assert.equal(s.document.getElementById('not_card').options.length,1);assert.equal(s.document.getElementById('outcome').value,'loss');assert.equal(s.document.getElementById('phase').value,'side_second');assert.equal(s.calls.at(-1),'load');
});
test('failed card candidates leave deck candidates usable',async()=>{
 const s=recordingSetup('card-table');await new Promise(r=>setImmediate(r));assert.equal(s.document.getElementById('deck-list').options[0].value,'雷盟');assert.match(s.document.getElementById('cardSearch').dataset.candidateError,/暂不可用/);
});
test('report ignores stale response and Escape invalidates in-flight candidates',async()=>{
 const s=setup();for(const id of ['q','list','chips','hint'])s.make(id);const pending=[];Object.assign(s.context,{$:s.document.getElementById,SS:{el:(tag,text,cls)=>Object.assign(new s.E(tag),{textContent:text,className:cls}),api:()=>new Promise(resolve=>pending.push(resolve))}});
 s.context.el=s.context.SS.el;const src=read('statistics_cards_report.js');vm.runInContext(src.slice(src.indexOf('function createPicker('),src.indexOf('const targets =')),s.context);const p=s.context.createPicker({input:'q',list:'list',chips:'chips',hint:'hint',hintText:'{n}'}),input=s.document.getElementById('q');
 const a=p.search('旧'),b=p.search('新');pending[1]({cards:[{code:2,name:'新'}]});await b;pending[0]({cards:[{code:1,name:'旧'}]});await a;assert.equal(p.suggest[0].code,2);
 const c=p.search('灰');s.fire(input,'keydown',{key:'Escape'});pending[2]({cards:[{code:3,name:'灰流丽'}]});await c;assert.equal(s.document.getElementById('list').hidden,true);assert.equal(p.suggest.length,0);
});

test('investment search ignores old responses and loads statistics only after choosing',async()=>{
 const s=setup(),input=s.make('q'),pending=[];let loaded=0;
 Object.assign(s.context,{$:s.document.getElementById,api:()=>new Promise(resolve=>pending.push(resolve)),load:()=>loaded++});
 const html=read('statistics_investment.html'),start=html.indexOf('let cardCandidates=');vm.runInContext(html.slice(start,html.indexOf('async function load()',start)),s.context);
 input.value='旧';input.focus();const a=s.context.find();input.value='灰';const b=s.context.find();pending[1]({cards:[{code:14558127,name:'灰流丽'}]});await b;pending[0]({cards:[{code:1,name:'旧'}]});await a;
 const list=input.parent.children[1];assert.equal(list.children[0].children[0].textContent,'灰流丽');assert.equal(loaded,0);s.fire(list.children[0],'click');assert.equal(loaded,1);
});
