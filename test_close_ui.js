'use strict';
// Deterministic coordinator/DOM-text tests. No browser, network or live quotes.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const E=require('./engine.js');
const original=fs.readFileSync('app.js','utf8');
const hook=`globalThis.testAPI={state,clock,acceptMarketStatus,completionState,renderMarketClock,cnParts,serverNow,maybeAutoRefresh,syncMarketStatus,tickMarketClock,fetchOnline,cancelRequest,replaceFetch(fn){fetchOnline=fn},replaceApply(fn){applyDataset=fn},replaceSave(fn){savePrefs=fn}};`;
const source=original.replace("document.addEventListener('DOMContentLoaded',init);",hook);
assert.notEqual(source,original,'test seam must replace only automatic init');
function setup(){
  let elapsed=0;
  const elements=new Map(),listeners=[];
  const element=id=>{if(!elements.has(id))elements.set(id,{id,value:'',textContent:'',className:'',disabled:false,hidden:false,attrs:{},setAttribute(k,v){this.attrs[k]=String(v)},focus(){},classList:{add(){},remove(){}}});return elements.get(id);};
  element('symbol-input').value='600519.SS';element('range-input').value='2y';
  const context={console,Intl,Date,URLSearchParams,AbortController,ChanEngine:E,performance:{now:()=>elapsed},setTimeout:()=>1,clearTimeout(){},setInterval(){},document:{hidden:false,getElementById:element,addEventListener(...args){listeners.push(args)}},location:{protocol:'http:',hostname:'127.0.0.1'},localStorage:{setItem(){},getItem(){return null}},fetch:async()=>{throw new Error('unexpected fetch')},window:{addEventListener(){}}};
  vm.runInNewContext(source,context,{filename:'app.js'});
  const t=context.testAPI;
  t.state.source={kind:'online',code:'601899.SS',name:'紫金矿业',label:'测试',currency:'CNY',meta:{readiness:'ready',range:'2y'}};
  t.state.bars=[{time:'2026-11-03',close:20},{time:'2026-11-04',close:21}];t.state.analysis={signals:[]};
  return {context,t,element,setElapsed:v=>elapsed=v};
}
const status=(serverTime='2026-11-05T00:01:00+08:00',targetDate='2026-11-04',phase='preopen')=>({serverTime,targetDate,phase,timezone:'Asia/Shanghai',isTradingDay:true,policy:'cn_close_v1',marketCloseTime:'15:00'});
const tests=[];
function test(name,fn){tests.push({name,fn});}
test('server clock is rendered in Beijing time, including midnight, independent of host timezone',()=>{
  const {t,element,setElapsed}=setup();t.acceptMarketStatus(status());setElapsed(2500);t.renderMarketClock();assert.equal(element('market-clock').textContent,'2026-11-05 00:01:02');assert.equal(element('market-target').textContent,'2026-11-04');assert.equal(element('market-actual').textContent,'2026-11-04');
});
test('target day pending never presents previous-day B signal as a new target signal',()=>{
  const {t,element}=setup();t.acceptMarketStatus(status('2026-11-05T15:00:05+08:00','2026-11-05','closed'));t.state.source.meta.readiness='awaiting_source';t.state.analysis.signals=[{type:'B1',index:1}];t.renderMarketClock();assert.equal(t.completionState().ready,false);assert.equal(element('session-b-points').textContent,'待完整日K');assert.match(element('market-data-ready').textContent,/等待数据源发布 2026-11-05/);assert.match(element('market-data-ready').textContent,/当前仅计算至 2026-11-04/);
});
test('ready day shows only B points confirmed on its exact completed session',()=>{
  const {t,element}=setup();t.acceptMarketStatus(status());t.state.analysis.signals=[{type:'B2',index:0},{type:'B1',index:1},{type:'B3',index:1},{type:'S1',index:1}];t.renderMarketClock();assert.equal(element('session-b-points').textContent,'B1 / B3');assert.equal(t.completionState().ready,true);
});
test('late history response cannot roll a newer market clock back across the close boundary',()=>{
  const {t}=setup();t.acceptMarketStatus(status('2026-11-05T15:00:01+08:00','2026-11-05','closed'));
  assert.equal(t.acceptMarketStatus(status('2026-11-05T14:59:59+08:00','2026-11-04','afternoon'),true),false);
  assert.equal(t.clock.status.targetDate,'2026-11-05');assert.equal(t.clock.status.phase,'closed');
});
test('CSV, snapshot, sample and foreign-market data cannot be replaced by automatic A-share refresh',()=>{
  const {t}=setup();t.acceptMarketStatus(status());let calls=0;t.replaceFetch(()=>calls++);
  for(const kind of ['csv','snapshot','sample']){t.state.source.kind=kind;assert.equal(t.maybeAutoRefresh('boundary','2026-11-03'),false);}
  t.state.source.kind='online';t.state.source.code='AAPL';assert.equal(t.maybeAutoRefresh('boundary','2026-11-03'),false);assert.equal(calls,0);
});
test('close target change triggers one refresh and does not overlap an active request',()=>{
  const {t}=setup();t.acceptMarketStatus(status('2026-11-05T15:00:01+08:00','2026-11-05','closed'));t.clock.lastAutoAttempt=0;let calls=0;t.replaceFetch(()=>{calls++;t.clock.requestActive=true});assert.equal(t.maybeAutoRefresh('boundary','2026-11-04'),true);assert.equal(t.maybeAutoRefresh('poll','2026-11-04'),false);assert.equal(calls,1);
});
test('source-awaiting retries are throttled to two minutes and honor cancellation',()=>{
  const {t,setElapsed}=setup();t.acceptMarketStatus(status());t.state.source.meta.readiness='awaiting_source';let calls=0;t.replaceFetch(()=>calls++);assert.equal(t.maybeAutoRefresh(),true);setElapsed(60000);assert.equal(t.maybeAutoRefresh(),false);setElapsed(120000);assert.equal(t.maybeAutoRefresh(),true);t.cancelRequest();setElapsed(180000);assert.equal(t.maybeAutoRefresh(),false);assert.equal(calls,2);
});
test('midnight refreshes the online instrument even when the latest completed target is unchanged',()=>{
  const {t}=setup();t.acceptMarketStatus(status());let calls=0;t.replaceFetch(()=>calls++);assert.equal(t.maybeAutoRefresh('boundary','2026-11-04','2026-11-04'),true);assert.equal(calls,1);
});
test('failed clock sync retains bars but marks new-day judgement unavailable',async()=>{
  const {t,context,element}=setup();t.acceptMarketStatus(status());const bars=t.state.bars;context.fetch=async()=>{throw new Error('Failed to fetch')};await t.syncMarketStatus();assert.equal(t.state.bars,bars);assert.equal(t.completionState().ready,false);assert.match(element('market-data-ready').textContent,/时间核验不可用/);assert.equal(t.clock.pending,false);
});
test('simultaneous clock polls do not issue duplicate calls',async()=>{
  const {t,context}=setup();let resolve,calls=0;context.fetch=()=>{calls++;return new Promise(r=>resolve=r)};const first=t.syncMarketStatus();await t.syncMarketStatus();assert.equal(calls,1);resolve({ok:true,json:async()=>status()});await first;assert.equal(t.clock.pending,false);
});
test('15:00 crossing causes an immediate time check rather than waiting for minute interval',async()=>{
  const {t,context,setElapsed}=setup();t.acceptMarketStatus(status('2026-11-05T14:59:59+08:00','2026-11-04','afternoon'));let calls=0;context.fetch=async()=>{calls++;return {ok:true,json:async()=>status('2026-11-05T15:00:00+08:00','2026-11-05','closed')}};t.replaceFetch(()=>{});setElapsed(1000);t.tickMarketClock();await new Promise(setImmediate);assert.equal(calls,1);assert.equal(t.clock.status.targetDate,'2026-11-05');
});
test('automatic history uses the loaded symbol/range, not unsent input edits, and forces a fresh request',async()=>{
  const {t,context,element}=setup();t.acceptMarketStatus(status());element('symbol-input').value='000001.SZ';element('range-input').value='6mo';let url='';context.fetch=async u=>{url=u;return {ok:false,json:async()=>({error:'test source error'})}};const old=t.state.bars;await t.fetchOnline(null,{automatic:true});assert.match(url,/symbol=601899.SS/);assert.match(url,/range=2y/);assert.match(url,/refresh=1/);assert.equal(element('symbol-input').value,'000001.SZ');assert.equal(t.state.bars,old);assert.match(element('request-status').textContent,/当前数据未替换/);assert.equal(t.clock.requestActive,false);
});
test('awaiting history status preserves the real cutoff instead of announcing fresh complete data',async()=>{
  const {t,context,element}=setup();t.acceptMarketStatus(status('2026-11-05T15:00:05+08:00','2026-11-05','closed'));
  const bars=E.sampleData(5).map((b,i)=>({...b,time:['2026-10-29','2026-10-30','2026-11-02','2026-11-03','2026-11-04'][i]}));
  context.fetch=async()=>({ok:true,json:async()=>({bars,meta:{symbol:'600519.SS',range:'2y',readiness:'awaiting_source',expectedThrough:'2026-11-05',marketStatus:status('2026-11-05T15:00:05+08:00','2026-11-05','closed')}})});
  t.replaceApply((rows,source)=>{t.state.bars=rows;t.state.source=source;t.state.analysis={signals:[]}});t.replaceSave(()=>{});await t.fetchOnline();assert.match(element('request-status').textContent,/实际截止 2026-11-04/);assert.match(element('request-status').textContent,/等待数据源发布 2026-11-05/);assert.equal(element('session-b-points').textContent,'待完整日K');
});
test('clock markup has unique required fields and a mobile two-column treatment',()=>{
  const html=fs.readFileSync('index.html','utf8'),css=fs.readFileSync('styles.css','utf8');for(const id of ['market-clock','market-phase','market-target','market-actual','session-b-points','market-data-ready','market-sync-status'])assert.equal(html.split('id="'+id+'"').length-1,1,id);assert.match(css,/@media\(max-width:540px\)\{\.session-clock/);assert.match(original,/visibilitychange/);assert.match(original,/addEventListener\('focus'/);
});
(async()=>{for(const t of tests){await t.fn();console.log('PASS '+t.name)}console.log(JSON.stringify({passed:tests.length,scope:'UI clock coordinator and DOM-text mocks; no visual browser validation',appSha256:require('node:crypto').createHash('sha256').update(original).digest('hex')},null,2))})().catch(error=>{console.error(error);process.exitCode=1});
