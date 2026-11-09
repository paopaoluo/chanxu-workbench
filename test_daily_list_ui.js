'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),E=require('./engine.js');
const original=fs.readFileSync('app.js','utf8');
const hook=`globalThis.t={state,clock,daily,dailyCandidates,filteredDailyCandidates,dailyPage,dailyReportState,renderDailyList,loadDailyList,openDailySnapshot,matchDailySignal,dailyCSV,csvSafeText,beginRequest,cancelRequest,loadInitialOnline,navigate,replaceApply(fn){applyDataset=fn},replaceLocate(fn){locateSignal=fn},replaceSave(fn){savePrefs=fn},replaceFetch(fn){fetchOnline=fn}};`;
const source=original.replace("document.addEventListener('DOMContentLoaded',init);",hook);
function setup(){
  const elements=new Map(),nodes=[];let fetchCount=0;
  const el=id=>{if(!elements.has(id))elements.set(id,{id,value:'',innerHTML:'',textContent:'',className:'',disabled:false,hidden:false,attrs:{},setAttribute(k,v){this.attrs[k]=String(v)},scrollIntoView(){this.scrolled=true},focus(){},classList:{add(){},remove(){},toggle(){}}});return elements.get(id)};
  const ctx={console,Intl,Date,URLSearchParams,AbortController,ChanEngine:E,performance:{now:()=>0},setTimeout:()=>1,clearTimeout(){},setInterval(){},document:{hidden:false,getElementById:el,querySelectorAll:()=>nodes,addEventListener(){}},location:{protocol:'http:',hostname:'127.0.0.1',hash:''},history:{replaceState(_a,_b,hash){ctx.location.hash=hash}},matchMedia:()=>({matches:true}),window:{addEventListener(){},scrollTo(options){ctx.scrolledTo=options.top}},localStorage:{setItem(){}},fetch:async()=>{fetchCount++;throw new Error('unexpected request')}};
  vm.runInNewContext(source,ctx);return {t:ctx.t,ctx,el,nodes,fetchCount:()=>fetchCount};
}
const s=(type='B1',extra={})=>({id:type+'-3',type,confirmationDate:'2026-11-04',price:9,stop:8,distanceToStopPct:20,...extra});
const c=(code='600012',extra={})=>({code,name:'皖通高速',symbol:code+'.SS',close:10,flags:[],signals:[s()],...extra});
const payload=(rows=[c()],extra={})=>({schema:'chanxu-daily-signals-v1',expectedTargetDate:'2026-11-04',reportState:'current',report:{reportKey:'run-key',reportSha256:'a'.repeat(64),targetDate:'2026-11-04',generatedAt:'2026-11-05T02:00:00+08:00',status:'complete',coverage:{ok:3053,inScope:3053,failed:0,unsupported:0},candidates:rows},summary:{candidateCount:rows.length,signalCount:rows.reduce((n,r)=>n+r.signals.length,0),flaggedCount:0,uncoveredReasons:[]},progress:{freshness:'unknown'},...extra});
const tests=[];function test(name,fn){tests.push({name,fn})}
test('filter retains original stock order and all multi-type signals while deduplicating stocks',()=>{
  const {t}=setup();t.daily.payload=payload([c('600012',{signals:[s('B1'),s('B3')]}),c('601088',{name:'中国神华',signals:[s('B3')]}),c('600012',{signals:[s('B1')]})]);
  assert.equal(t.dailyCandidates().length,2);assert.equal(t.dailyCandidates()[0].signals.length,2);t.daily.type='B3';assert.equal(t.filteredDailyCandidates().map(x=>x.code).join(','),'600012,601088');assert.equal(t.filteredDailyCandidates()[0].signals.length,2);
});
test('name/code search and clean-only filter work together',()=>{
  const {t}=setup();t.daily.payload=payload([c('600012'),c('601088',{name:'中国神华',flags:['历史不足120根']})]);t.daily.query='神华';assert.equal(t.filteredDailyCandidates().length,1);t.daily.clean=true;assert.equal(t.filteredDailyCandidates().length,0);t.daily.query='600012';assert.equal(t.filteredDailyCandidates().length,1);
});
test('pagination uses 10 unique stocks, clamps empty pages, and preserves report order',()=>{
  const {t}=setup();t.daily.payload=payload(Array.from({length:21},(_,i)=>c(String(600000+i))));assert.equal(t.dailyPage().visible.length,10);t.daily.page=3;assert.equal(t.dailyPage().visible.length,1);assert.equal(t.dailyPage().visible[0].code,'600020');t.daily.query='missing';assert.equal(t.dailyPage().pages,1);assert.equal(t.daily.page,1);
});
test('old partial report is explicitly marked old and uncovered is not no-signal',()=>{
  const {t,el}=setup();const p=payload();p.expectedTargetDate='2026-11-05';p.report.status='partial';p.report.coverage={ok:2222,inScope:3053,failed:831,unsupported:0};t.daily.payload=p;t.renderDailyList();assert.match(el('daily-status').textContent,/旧报告/);assert.match(el('daily-status').textContent,/部分覆盖/);assert.match(el('daily-status').textContent,/831 只未覆盖/);assert.equal(el('daily-coverage').textContent,'2222 / 3053');assert.match(el('daily-state-badge').textContent,/旧报告/);
});
test('counts are stocks vs signals, not conflated',()=>{
  const {t,el}=setup();t.daily.payload=payload([c('600012',{signals:[s('B1'),s('B3')]}),c('601088')]);t.renderDailyList();assert.equal(el('daily-candidate-count').textContent,2);assert.equal(el('daily-signal-count').textContent,3);
});
test('finished progress never implies that the scanner may still be running',()=>{
  const {t,el}=setup();t.daily.payload=payload();t.daily.payload.progress={status:'finished',freshness:'stale',processed:3053,requested:3053,targetDate:'2026-11-04'};t.renderDailyList();assert.match(el('daily-progress').textContent,/已结束/);assert.doesNotMatch(el('daily-progress').textContent,/进程仍在运行/);t.daily.payload.progress.status='running';t.renderDailyList();assert.match(el('daily-progress').textContent,/不能据此确认进程仍在运行/);
});
test('newer close-clock target relabels a current API report as historical before the next report poll',()=>{
  const {t,el}=setup();t.daily.payload=payload();t.clock.status={targetDate:'2026-11-05'};t.renderDailyList();assert.equal(t.dailyReportState(),'outdated');assert.match(el('daily-state-badge').textContent,/旧报告/);assert.equal(el('daily-target-date').textContent,'2026-11-05');assert.match(el('daily-status').textContent,/不是当前目标日/);
});
test('missing, loading, no candidates, no filter matches, and invalid report have distinct text',()=>{
  const {t,el}=setup();t.daily.loading=true;t.renderDailyList();assert.match(el('daily-status').textContent,/正在读取/);t.daily.loading=false;t.daily.payload=payload([],{report:null,reportState:'missing'});t.renderDailyList();assert.match(el('daily-status').textContent,/还没有正式/);t.daily.payload=payload([]);t.renderDailyList();assert.match(el('daily-rows').innerHTML,/成功分析范围内没有新 B 点/);t.daily.payload=payload();t.daily.query='不存在';t.renderDailyList();assert.match(el('daily-rows').innerHTML,/当前筛选没有匹配/);t.daily.payload=payload([],{report:null,reportState:'invalid'});t.renderDailyList();assert.match(el('daily-status').textContent,/未通过校验/);
});
test('failed refresh preserves old rows and announces failure, never says latest',async()=>{
  const {t,el,ctx}=setup();t.daily.payload=payload();ctx.fetch=async()=>{throw new Error('Failed to fetch')};await t.loadDailyList();assert.match(el('daily-rows').innerHTML,/皖通高速/);assert.match(el('daily-status').textContent,/清单读取失败/);assert.match(el('daily-status').textContent,/不代表清单已更新/);assert.equal(el('daily-refresh').disabled,false);
});
test('daily polling is read-only, hidden-tab guarded and non-overlapping',async()=>{
  const {t,ctx}=setup();let resolve,calls=[];ctx.fetch=url=>{calls.push(url);return new Promise(r=>resolve=r)};ctx.document.hidden=true;await t.loadDailyList();assert.equal(calls.length,0);ctx.document.hidden=false;const first=t.loadDailyList();await t.loadDailyList();assert.deepEqual(calls,['/api/daily-signals']);resolve({ok:true,json:async()=>payload()});await first;assert.equal(t.daily.loading,false);
});
test('malformed candidate payload leaves the prior usable table intact',async()=>{
  const {t,ctx,el}=setup();t.daily.payload=payload();const bad=payload();bad.report.candidates=[null];ctx.fetch=async()=>({ok:true,json:async()=>bad});await t.loadDailyList();assert.match(el('daily-rows').innerHTML,/皖通高速/);assert.match(el('daily-status').textContent,/读取失败/);
});
test('CSV exports only current filtered stocks with safe formula text, quoting and preserved leading zero code',()=>{
  const {t}=setup();t.daily.payload=payload([c('000001',{name:'=HYPERLINK("bad")',signals:[s('B3')],flags:['+bad,\ntext']}),c('600012')]);t.daily.type='B3';const csv=t.dailyCSV();assert.match(csv,/'000001/);assert.match(csv,/'=HYPERLINK\(""bad""\)/);assert.match(csv,/'\+bad,/);assert.doesNotMatch(csv,/600012/);assert.equal(t.csvSafeText(' @cmd'),"' @cmd");
});
function snapshotFixture(){const all=E.sampleData(240),sig=E.analyze(all).signals.find(s=>s.type.startsWith('B')&&s.index>10),bars=all.slice(0,sig.index+1),analysis=E.analyze(bars),actual=analysis.signals.find(s=>s.type===sig.type&&s.index===sig.index);assert(actual);const wanted={...actual,id:'deliberately-not-the-confirmation-index',confirmationDate:bars[actual.index].time,pivotDate:bars[actual.pivotIndex].time};const candidate=c('600012',{signals:[wanted]});const p=payload([candidate]);p.report.targetDate=bars.at(-1).time;p.expectedTargetDate=p.report.targetDate;return {bars,analysis,actual,candidate,p};}
test('snapshot uses report key and sha, recomputes frozen bars, matches confirmation evidence rather than id suffix',async()=>{
  const {t,ctx,el}=setup(),f=snapshotFixture();t.daily.payload=f.p;let url,applied,located;ctx.fetch=async u=>{url=u;return {ok:true,json:async()=>({bars:f.bars,candidate:f.candidate,meta:{symbol:'600012.SS',currency:'CNY'},report:f.p.report,engineCompatible:true})}};
  t.state.backtest={old:true};t.replaceApply((bars,source)=>{applied={bars,source};t.state.bars=bars;t.state.source=source;t.state.analysis=E.analyze(bars);t.state.backtest=null});t.replaceLocate(id=>{located=id;t.state.view='chart'});t.replaceSave(()=>{});await t.openDailySnapshot('600012');
  assert.match(url,/^\/api\/daily-signals\/snapshot\?/);assert.match(url,/report=run-key/);assert.match(url,/sha=aaaa/);assert.match(url,/code=600012/);assert.equal(applied.source.kind,'snapshot');assert.equal(applied.source.meta.reportTargetDate,f.p.report.targetDate);assert.equal(located,f.actual.id);assert.equal(t.state.backtest,null);assert.equal(t.state.view,'chart');assert.equal(el('chart-date').value,f.p.report.targetDate);assert.match(el('request-status').textContent,/未拉取最新行情/);
});
test('incompatible or changed snapshot does not replace current data',async()=>{
  const {t,ctx,el}=setup(),f=snapshotFixture();t.daily.payload=f.p;const prior=t.state.bars;ctx.fetch=async()=>({ok:true,json:async()=>({bars:f.bars,candidate:f.candidate,report:f.p.report,engineCompatible:false})});await t.openDailySnapshot('600012');assert.equal(t.state.bars,prior);assert.match(el('request-status').textContent,/版本或算法校验不一致/);
});
test('manual newer history request wins over late daily snapshot response',async()=>{
  const {t,ctx}=setup(),f=snapshotFixture();t.daily.payload=f.p;let resolve,applied=0;ctx.fetch=()=>new Promise(r=>resolve=r);t.replaceApply(()=>applied++);const request=t.openDailySnapshot('600012');const newJob=t.beginRequest('用户手动新标的');resolve({ok:true,json:async()=>({bars:f.bars,candidate:f.candidate,report:f.p.report,engineCompatible:true})});await request;assert.equal(applied,0);assert.equal(t.state.requestId,newJob.id);assert.equal(t.clock.requestActive,true);
});
test('cancelled daily snapshot and delayed initial fetch cannot overwrite user-selected data',async()=>{
  const {t,ctx}=setup(),f=snapshotFixture();t.daily.payload=f.p;let resolve,applied=0,initialFetches=0;ctx.fetch=()=>new Promise(r=>resolve=r);t.replaceApply(()=>applied++);t.replaceFetch(()=>initialFetches++);const initialId=t.state.requestId;const request=t.openDailySnapshot('600012');t.cancelRequest();resolve({ok:true,json:async()=>({bars:f.bars,candidate:f.candidate,report:f.p.report,engineCompatible:true})});await request;t.loadInitialOnline(null,initialId);assert.equal(applied,0);assert.equal(initialFetches,0);t.state.source.kind='snapshot';t.loadInitialOnline(null,t.state.requestId);assert.equal(initialFetches,0);
});
test('daily anchor navigation updates title, active item and location without a page load',()=>{
  const {t,ctx,el,nodes}=setup();let active='';nodes.push({dataset:{view:'daily'},classList:{toggle(_a,value){if(value)active='daily'}}});t.replaceSave(()=>{});t.navigate('daily');assert.equal(ctx.location.hash,'#daily');assert.equal(t.state.view,'daily');assert.equal(el('page-title').textContent,'新B点清单');assert.equal(el('daily-view').hidden,false);assert.equal(el('chart-view').hidden,true);assert.equal(ctx.scrolledTo,0);assert.equal(active,'daily');t.navigate('chart');assert.equal(el('page-title').textContent,'结构图');
});
test('markup places daily section after backtest and before rules, with scroll-isolated table and 44px controls',()=>{
  const html=fs.readFileSync('index.html','utf8'),css=fs.readFileSync('styles.css','utf8');assert(html.indexOf('id="daily-view"')>html.indexOf('id="backtest-view"'));assert(html.indexOf('id="daily-view"')<html.indexOf('id="rules-view"'));assert(html.includes('data-view="daily"'));assert.match(css,/\.daily-table-scroll\{[^}]*max-width:100%[^}]*overflow:auto/);assert.match(css,/\.rail\{display:grid;grid-template-columns:repeat\(5,minmax\(0,1fr\)\)/);assert.match(css,/\.daily-open\{min-height:44px/);
});
(async()=>{for(const test of tests){await test.fn();console.log('PASS '+test.name)}console.log(JSON.stringify({passed:tests.length,scope:'deterministic UI/contract mocks, including original engine snapshot replay; visual browser verification is separate'},null,2))})().catch(error=>{console.error(error);process.exitCode=1});


