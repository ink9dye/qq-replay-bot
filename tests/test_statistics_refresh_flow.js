const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync('bot/statistics_refresh.js','utf8');
function setup({readyAt=45000,changeAt=Infinity,stale=false}={}) {
  let now=0,calls=0,events=0,notices=[],form={value:'A',type:'text'};
  const note={hidden:false,set textContent(s){notices.push(s)}};
  const context={URL,DOMException,CustomEvent:class {constructor(t,o){this.type=t;this.detail=o.detail;}},Date:class extends Date {static now(){return now;}},
    location:{href:'http://localhost/statistics/cards-report',origin:'http://localhost'},
    document:{hidden:false,querySelectorAll:()=>[form],getElementById:()=>note},
    setTimeout(fn,ms){now+=ms;if(now>=changeAt)form.value='B';queueMicrotask(fn);return 1;},clearTimeout(){}};
  context.window={fetch:async()=>{calls++;return {status:now<readyAt?503:200,ok:now>=readyAt,clone(){return this;},json:async()=>({code:'query_busy'}),headers:{get:k=>k==='X-Statistics-State'?(stale?'stale':'fresh'):null}};},dispatchEvent(){events++;}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,read:()=>({now,calls,notices,events})};
}
test('cold result beyond the old 40 second window succeeds with bounded backoff',async()=>{
  const s=setup();const r=await s.context.window.fetch('/statistics/api/card-report');assert.equal(r.status,200);assert.ok(s.read().now>=45000);assert.ok(s.read().calls<12);
});
test('changed conditions cancel waiting before another network request',async()=>{
  const s=setup({changeAt:3000});await assert.rejects(s.context.window.fetch('/statistics/api/card-report'),{name:'AbortError'});assert.equal(s.read().calls,1);
});
test('persistent busy stops after ten minutes without a tight polling loop',async()=>{
  const s=setup({readyAt:Infinity});const r=await s.context.window.fetch('/statistics/api/card-report');assert.equal(r.status,503);assert.ok(s.read().calls<30);assert.ok(s.read().notices.at(-1).includes('等待已结束'));
});
