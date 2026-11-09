// Offline DOM/event regression tests. Browser rendering is verified separately.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const E = require('./engine.js');
const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
const app = fs.readFileSync(__dirname + '/app.js', 'utf8');

class Element {
  constructor(tag = 'div') { this.tagName=tag;this.events={};this.children=[];this.attrs={};this.dataset={};this.className='';this.innerHTML='';this.textContent='';this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.style={};this.parentElement={clientWidth:1000};this.classList={add(){},remove(){},toggle(){}}; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute(k,v) { this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v); }
  getBoundingClientRect() { return {left:0,top:0,width:1000,height:450}; }
  focus() { this.focused=true; }
  scrollIntoView() {}
  click() { if(this.download)this.downloaded=true;return this.dispatch('click'); }
  async dispatch(type, event={}) { for(const fn of this.events[type] || [])await fn({target:this,key:'',preventDefault(){},...event}); }
}

function harness({storage=new Map(),engine=E,fetcher=async()=>{throw new Error('Failed to fetch');}}={}) {
  const nodes=[],ids=new Map(),handlers={},downloads=[],urls=new Map();let urlCounter=0;
  for(const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
    const node=new Element(match[1]);const attributes=match[2];
    for(const attr of attributes.matchAll(/([\w-]+)="([^"]*)"/g))node.setAttribute(attr[1],attr[2]);
    node.id=node.attrs.id;node.value=node.attrs.value||'';node.checked=/\bchecked\b/.test(attributes);node.hidden=/\bhidden\b/.test(attributes);
    if(node.id){assert(!ids.has(node.id),'duplicate DOM ID '+node.id);ids.set(node.id,node);}nodes.push(node);
  }
  ids.get('range-input').value='2y';
  const document={getElementById:id=>ids.get(id)||null,createElementNS:(_ns,name)=>new Element(name),createElement:name=>{const n=new Element(name);if(name==='a')n.click=function(){downloads.push({name:this.download,blob:urls.get(this.href)});};return n;},querySelectorAll:selector=>selector==='[data-layer]'?nodes.filter(n=>n.dataset.layer):selector==='[data-window]'?nodes.filter(n=>n.dataset.window):selector==='[data-view]'?nodes.filter(n=>n.dataset.view):selector==='.backtest-controls input'?['capital','position','risk','lot','fee','slippage','t1'].map(id=>ids.get(id+'-input')):[],querySelector:()=>new Element(),addEventListener:(type,fn)=>{handlers[type]=fn;}};
  const URLMock=class extends URL {};URLMock.createObjectURL=blob=>{const key='blob:'+urlCounter++;urls.set(key,blob);return key;};URLMock.revokeObjectURL=()=>{};
  const location={protocol:'file:',hostname:'',hash:''};
  const context={document,ChanEngine:engine,console,location,history:{replaceState(_a,_b,hash){location.hash=hash;}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setTimeout(){return 0;},clearTimeout(){},setInterval(){return 0;},clearInterval(){},performance:{now:()=>0},ResizeObserver:class{observe(){}},matchMedia:()=>({matches:true}),URL:URLMock,URLSearchParams,Blob,Intl,AbortController,TextEncoder,Uint8Array,fetch:(url,options)=>url==='/api/watchlist'?Promise.resolve({ok:true,json:async()=>({schema:'chanxu-watchlist-v1',revision:0,updatedAt:null,items:[],storage:'local_server'})}):url==='/api/daily-signals'?Promise.resolve({ok:true,json:async()=>({schema:'chanxu-daily-signals-v1',reportState:'missing',report:null,summary:{uncoveredReasons:[]},progress:{freshness:'unknown'}})}):fetcher(url,options),window:{crypto:webcrypto,addEventListener(){},scrollTo(){}}};
  vm.runInNewContext(app,context,{filename:'app.js'});handlers.DOMContentLoaded();
  return {ids,nodes,downloads,storage,get:id=>ids.get(id),signals:()=>ids.get('price-chart').children.filter(n=>n.attrs.class==='chart-signal')};
}
const csv = bars => 'time,open,high,low,close,volume\n'+bars.map(b=>[b.time,b.open,b.high,b.low,b.close,b.volume].join(',')).join('\n');
const file = (name,text) => ({name,size:Buffer.byteLength(text),text:async()=>text});
const response=(bars,meta={})=>({ok:true,json:async()=>({bars,meta:{symbol:'510300.SS',name:'测试ETF',source:'离线测试',currency:'CNY',...meta}})});
const tick=()=>new Promise(resolve=>setImmediate(resolve));

async function main() {
  const checks=[];
  const h=harness(),old=h.signals()[0];assert(old);await old.dispatch('click');assert(h.get('signal-detail').innerHTML.includes('确认时间'));
  await h.get('backtest-btn').dispatch('click');assert.equal(h.get('export-trades').disabled,false);assert(h.get('equity-readout').textContent.includes('净值'));
  const other=E.sampleData(5).map((b,i)=>({...b,time:'2030-01-'+String(i+1).padStart(2,'0'),open:b.open+200,high:b.high+200,low:b.low+200,close:b.close+200}));
  h.get('file-input').files=[file('NEW.csv',csv(other))];await h.get('file-input').dispatch('change');
  assert.equal(h.get('latest-date').textContent,'2030-01-05');assert.equal(h.get('latest-signal').textContent,'暂无确认信号');assert(!h.get('signal-detail').innerHTML.includes('确认时间'));assert.equal(h.get('export-trades').disabled,true);assert.equal(h.get('equity-readout').textContent,'');assert(h.get('data-freshness').textContent.includes('完成度需自行核实'));
  checks.push('R02 CSV dataset replacement clears old signal evidence and backtest');

  const fake={...E,analyze(bars,...args){const a=E.analyze(bars,...args);if(bars.length===240){const s={id:'B1-DUP',type:'B1',index:220,pivotIndex:219,price:bars[219].low,stop:bars[219].low,reason:'离线重叠样例'};a.signals=[s,{...s,id:'B2-DUP',type:'B2'}];}return a;}};
  const overlap=harness({engine:fake}),markers=overlap.signals();assert.equal(markers.length,2);assert.notEqual(markers[0].children[1].attrs.y,markers[1].children[1].attrs.y);
  await markers[0].dispatch('click');assert(overlap.get('signal-detail').innerHTML.includes('一买'));await overlap.signals()[1].dispatch('click');assert(overlap.get('signal-detail').innerHTML.includes('二买'));assert.equal(overlap.get('signal-select').children.length,3);
  checks.push('R03 same-index same-price signals have distinct marker lanes and independently selectable list entries');
  overlap.get('signal-select').value='';await overlap.get('signal-select').dispatch('change');assert(!overlap.get('signal-detail').innerHTML.includes('确认时间'));assert(overlap.signals().every(n=>n.attrs['aria-pressed']==='false'));assert.equal(JSON.parse(overlap.storage.get('chanxu.preferences.v1')).selectedSignalId,null);
  const clearedReload=harness({storage:overlap.storage,engine:fake});assert(!clearedReload.get('signal-detail').innerHTML.includes('确认时间'));assert(clearedReload.signals().every(n=>n.attrs['aria-pressed']==='false'));
  checks.push('Selection placeholder clears evidence, marker selection and persisted selection across reload');

  h.get('capital-input').value='0';await h.get('backtest-btn').dispatch('click');assert(h.get('capital-error').textContent.includes('需大于0'));assert(h.get('capital-input').focused);
  checks.push('R05 Chinese inline parameter error and focus');

  const pending=[];const race=harness({fetcher:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}))});
  race.get('symbol-input').value='SLOW';const first=race.get('fetch-btn').dispatch('click');race.get('symbol-input').value='FAST';const second=race.get('fetch-btn').dispatch('click');
  assert.equal(pending.length,2);assert(pending[0].options.signal.aborted);pending[1].resolve(response(E.sampleData(240),{symbol:'FAST',currency:'USD'}));await second;pending[0].resolve(response(E.sampleData(240),{symbol:'SLOW'}));await first;
  assert(race.get('instrument').textContent.includes('FAST'));assert.equal(race.get('currency-unit').textContent,'USD');
  await race.get('backtest-btn').dispatch('click');assert(race.get('account-breakdown').innerHTML.includes('USD'));
  checks.push('R13/R16 latest request wins despite out-of-order response; currency is USD');
  let cancelledRequest;
  const cancelled=harness({fetcher:(_url,options)=>new Promise(resolve=>{cancelledRequest={options,resolve};})}),beforeCancel=cancelled.get('instrument').textContent;
  cancelled.get('symbol-input').value='SLOW';const cancelledJob=cancelled.get('fetch-btn').dispatch('click');await cancelled.get('cancel-request').dispatch('click');
  assert(cancelledRequest.options.signal.aborted);assert.equal(cancelled.get('fetch-btn').disabled,false);assert.equal(cancelled.get('cancel-request').hidden,true);cancelledRequest.resolve(response(E.sampleData(240),{symbol:'SLOW'}));await cancelledJob;
  assert.equal(cancelled.get('instrument').textContent,beforeCancel);assert(cancelled.get('request-status').textContent.includes('已取消'));
  checks.push('R16 explicit cancel aborts request, resets controls and ignores a late successful response');

  const fail=harness();const sourceBefore=fail.get('source-status').textContent;await fail.get('fetch-btn').dispatch('click');assert.equal(fail.get('source-status').textContent,sourceBefore);assert(fail.get('request-status').textContent.includes('无法连接'));assert.equal(fail.get('data-indicator').className,'error');
  checks.push('R06 failed request preserves current data provenance and shows persistent error');

  const stored=harness();stored.get('capital-input').value='123000';await stored.get('capital-input').dispatch('input');await stored.get('backtest-btn').dispatch('click');assert(stored.get('equity-readout').textContent.includes('净值'));stored.get('capital-input').value='124000';await stored.get('capital-input').dispatch('input');assert.equal(stored.get('equity-readout').textContent,'');stored.get('capital-input').value='123000';await stored.get('capital-input').dispatch('input');await stored.get('backtest-btn').dispatch('click');await stored.signals()[0].dispatch('click');
  const reload=harness({storage:stored.storage});assert.equal(reload.get('capital-input').value,'123000');assert(reload.get('signal-detail').innerHTML.includes('确认时间'));assert.equal(reload.get('export-trades').disabled,true);
  checks.push('R12 preferences and same-dataset selected evidence restore; old backtest is not reused');

  const snap=harness();await snap.get('backtest-btn').dispatch('click');await snap.get('export-snapshot').dispatch('click');await tick();await tick();assert(snap.downloads.length);
  const exported=JSON.parse(await snap.downloads.at(-1).blob.text());assert.equal(exported.schema,'chanxu-research-v1');assert.equal(exported.hashAlgorithm,'SHA-256');assert(exported.dataHash&&exported.options&&exported.runId&&exported.ruleVersion);
  const imported=harness();imported.get('snapshot-input').files=[file('study.json',JSON.stringify(exported))];await imported.get('snapshot-input').dispatch('change');assert(imported.get('request-status').textContent.includes('完整性'));assert.equal(imported.get('export-trades').disabled,true);
  const corrupt=structuredClone(exported);corrupt.bars[0].close+=.01;imported.get('snapshot-input').files=[file('corrupt.json',JSON.stringify(corrupt))];const before=imported.get('instrument').textContent;await imported.get('snapshot-input').dispatch('change');assert(imported.get('request-status').textContent.includes('校验不一致'));assert.equal(imported.get('instrument').textContent,before);
  const injected=structuredClone(exported);injected.source.currency='<img src=x onerror=alert(1)>';injected.source.name='<img src=x onerror=alert(1)>';injected.ruleVersion='different';imported.get('snapshot-input').files=[file('untrusted.json',JSON.stringify(injected))];await imported.get('snapshot-input').dispatch('change');await imported.get('backtest-btn').dispatch('click');assert(!imported.get('account-breakdown').innerHTML.includes('<img'));assert(imported.get('request-status').textContent.includes('版本不同'));
  checks.push('R15 snapshot export/import integrity, rule mismatch, result invalidation and untrusted currency sanitization');
  console.log(JSON.stringify({passed:checks.length,checks,method:'offline DOM/event tests; real browser layout and native file chooser require separate verification'},null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
