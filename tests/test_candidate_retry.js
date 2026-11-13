const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const src=fs.readFileSync(require('node:path').join(__dirname,'../bot/statistics_samples.js'),'utf8');
const fn=src.slice(src.indexOf('async function api('),src.indexOf('function modal('));
test('candidate busy retries same URL with backoff; permissions do not retry',async()=>{
 for(const statuses of [[503,200],[403]]){
  const calls=[],delays=[];let n=0;
  const c={URLSearchParams,Date,Error,Promise,setTimeout:(fn,ms)=>{delays.push(ms);fn()},fetch:async(url)=>{
   calls.push(url);const status=statuses[n++];return{status,ok:status===200,clone:()=>({json:async()=>({code:'query_busy'})}),text:async()=>'denied',json:async()=>({rows:[1]})};
  }};vm.createContext(c);vm.runInContext(fn,c);
  if(statuses[0]===403){await assert.rejects(c.api('card-table'));assert.equal(calls.length,1)}
  else {const data=await c.api('card-table');assert.deepEqual(data.rows,[1]);assert.deepEqual(calls,['/statistics/api/card-table','/statistics/api/card-table']);assert.deepEqual(delays,[5000])}
 }
});
