'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const modulePath = path.join(__dirname, 'watchlist_monitor.js');
const target = '2026-11-04';
function monitor() { assert.ok(fs.existsSync(modulePath), 'watchlist_monitor.js must implement the pure summary contract'); return require(modulePath); }
function payload(symbol = '002156.SZ') {
  const days = ['2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01', '2026-11-02', '2026-11-03', target];
  return {bars: days.map(time => ({time, open: 10, high: 12, low: 9, close: 11, volume: 100})),
    meta: {symbol, currency: 'CNY', readiness: 'ready', stale: false, completenessPolicy: 'cn_close_v1', expectedThrough: target, asOf: target, interval: '1d', range: '2y'}};
}
function signal(type = 'B1', index = 7, extra = {}) { return {id: `${type}-999`, type, index, pivotIndex: Math.max(0, index - 1), price: 10, stop: 9, ...extra}; }
function analysis(signals = [signal()]) { return () => ({signals, ruleVersion: 'test-causal-v1'}); }
function run(p = payload(), signals = [signal()], date = target) { return monitor().analyzeHistory(p.meta.symbol, p, date, analysis(signals)); }

test('UMD exports Node and browser APIs', () => {
  assert.deepEqual(Object.keys(monitor()).sort(), ['analyzeHistory', 'isFreshBuy', 'sortItems']);
  const context = {self: {}}; vm.runInNewContext(fs.readFileSync(modulePath, 'utf8'), context);
  assert.equal(typeof context.self.WatchlistMonitor.analyzeHistory, 'function');
});
test('complete CN target returns actual close and newly confirmed B', () => {
  const result = run();
  assert.equal(result.close, 11); assert.equal(result.closeDate, target); assert.equal(result.currency, 'CNY');
  assert.equal(result.ready, true); assert.equal(result.status, 'ready'); assert.equal(result.isNewBuy, true);
  assert.equal(result.latestSignalDate, target); assert.equal(result.newBuySignals.length, 1); assert.equal(result.ruleVersion, 'test-causal-v1');
});
test('confirmation uses signal.index, not id, pivotIndex or confirmedAt', () => {
  const result = run(payload(), [signal('B1', 7, {id: 'B1-0', pivotIndex: 1, confirmedAt: 0})]);
  assert.equal(result.latestSignals[0].confirmationDate, target);
  assert.equal(result.latestSignals[0].pivotDate, '2026-10-29'); assert.equal(result.isNewBuy, true);
});
test('latest confirmation date, not array order, selects all simultaneous events', () => {
  const result = run(payload(), [signal('S2'), signal('B3'), signal('B1', 2)]);
  assert.deepEqual(result.latestSignals.map(s => s.type), ['S2', 'B3']);
  assert.equal(result.latestSignalDate, target); assert.equal(result.targetSignals.length, 2);
});
test('same-day B and S retained and B close<=stop is warned without removing B', () => {
  const result = run(payload(), [signal('B1', 7, {stop: 11}), signal('S1')]);
  assert.equal(result.latestSignals.length, 2); assert.equal(result.targetSignals.length, 2);
  assert.equal(result.newBuySignals.length, 1); assert.equal(result.isNewBuy, true);
  assert.match(result.message, /同日.*B.*S|同日.*买.*卖/); assert.match(result.message, /止损/);
});
test('historical B remains visible but cannot become a target-day B', () => {
  const result = run(payload(), [signal('B1', 5)]);
  assert.equal(result.latestSignalDate, '2026-11-02'); assert.equal(result.ready, true);
  assert.equal(result.targetSignals.length, 0); assert.equal(result.isNewBuy, false);
});
test('no signals still displays a valid close', () => {
  const result = run(payload(), []);
  assert.equal(result.close, 11); assert.equal(result.latestSignalDate, null);
  assert.deepEqual(result.latestSignals, []); assert.equal(result.isNewBuy, false);
});
test('stale history keeps actual dates/events, never new-buy eligibility', () => {
  const p = payload(); p.meta.stale = true;
  const result = run(p); assert.equal(result.status, 'stale'); assert.equal(result.closeDate, target);
  assert.equal(result.targetSignals.length, 1); assert.deepEqual(result.newBuySignals, []); assert.equal(result.ready, false);
});
test('pending source and old bars remain explicitly not ready', () => {
  const p = payload(); p.meta.readiness = 'awaiting_source'; p.bars.pop(); p.meta.asOf = '2026-11-03';
  const result = run(p, [signal('B2', 6)]);
  assert.equal(result.closeDate, '2026-11-03'); assert.equal(result.latestSignalDate, '2026-11-03');
  assert.equal(result.status, 'awaiting_source'); assert.equal(result.isNewBuy, false);
});
test('old policy, omitted stale flag and inconsistent source cutoff cannot be ready', () => {
  for (const change of [{completenessPolicy:'old'}, {stale:undefined}, {expectedThrough:'2026-11-03'}, {asOf:'2026-11-03'}, {completeThrough:'2026-11-03'}]) {
    const p=payload();Object.assign(p.meta,change);const result=run(p);
    assert.equal(result.ready,false);assert.equal(result.isNewBuy,false);
  }
});
test('payload for another symbol is invalid, not another card data', () => {
  const result=monitor().analyzeHistory('600186.SS',payload(),target,analysis());
  assert.equal(result.status,'invalid_data');assert.equal(result.close,null);assert.equal(result.isNewBuy,false);
});
test('CN future dates are removed before analysis, including uncompleted OHLC', () => {
  const p=payload();p.bars.push({time:'2026-11-05',open:null,high:null,low:null,close:null,volume:null});let received;
  const result=monitor().analyzeHistory(p.meta.symbol,p,target,bars=>{received=bars;return analysis()();});
  assert.equal(received.length,8);assert.equal(received.at(-1).time,target);assert.equal(result.closeDate,target);
  assert.equal(p.bars.length,9);
  p.meta.asOf='2026-11-05';p.meta.completeThrough='2026-11-05';
  const inconsistent=monitor().analyzeHistory(p.meta.symbol,p,target,analysis());
  assert.equal(inconsistent.closeDate,target);assert.equal(inconsistent.ready,false);assert.equal(inconsistent.isNewBuy,false);
});
test('invalid calendar dates, duplicated and out-of-order bars are rejected', () => {
  for(const edit of [p=>{p.bars[1].time='2026-02-30';},p=>{p.bars[1].time=p.bars[0].time;},p=>{p.bars.reverse();},p=>{p.bars[0].time='2026-11-01T00:00:00Z';}]){
    const p=payload();edit(p);let called=false;const result=monitor().analyzeHistory(p.meta.symbol,p,target,()=>{called=true;return analysis()();});
    assert.equal(result.status,'invalid_data');assert.equal(called,false);assert.equal(result.close,null);
  }
});
test('OHLC and volume strict: no missing, coercion, nonfinite or inconsistent fields', () => {
  for(const edit of [b=>{delete b.volume;},b=>{b.volume=-1;},b=>{b.volume='100';},b=>{b.close=NaN;},b=>{b.high=9;},b=>{b.open=0;},b=>{b.close=null;}]){
    const p=payload();edit(p.bars[3]);const result=run(p);assert.equal(result.status,'invalid_data');assert.equal(result.ready,false);
  }
});
test('invalid/unavailable target has historical display but cannot highlight', () => {
  for(const date of [null,undefined,'','2026-02-30','2026/11/04']){
    const result=monitor().analyzeHistory('002156.SZ',payload(),date,analysis());assert.equal(result.status,'calendar_unavailable');assert.equal(result.closeDate,target);
    assert.equal(result.targetDate,null);assert.equal(result.isNewBuy,false);
  }
});
test('non-CN history is displayed, not cut by CN target and never pinned', () => {
  const p=payload('AAPL');p.meta.currency='USD';p.meta.completenessPolicy='exclude_exchange_today';p.bars.push({...p.bars[7],time:'2026-11-05'});
  const result=run(p,[signal('B1',8)]);assert.equal(result.status,'foreign');assert.equal(result.ready,false);
  assert.equal(result.closeDate,'2026-11-05');assert.equal(result.latestSignalDate,'2026-11-05');assert.equal(result.isNewBuy,false);
});
test('15:00 target rollover dynamically removes old highlight without mutating summary', () => {
  const m=monitor(),summary=run(),before=JSON.stringify(summary);
  assert.equal(m.isFreshBuy(summary,target),true);assert.equal(m.isFreshBuy(summary,'2026-11-05'),false);
  assert.equal(m.isFreshBuy(summary,target,false),false);assert.equal(m.isFreshBuy(summary,null),false);
  assert.equal(JSON.stringify(summary),before);
});
test('fresh eligibility requires actual matching confirmed B, not stored isNewBuy flag', () => {
  const m=monitor(),summary=run();
  for(const changed of [{...summary,ready:false},{...summary,closeDate:'2026-11-03'}, {...summary,symbol:'AAPL'},
                       {...summary,newBuySignals:[]},{...summary,newBuySignals:[{type:'B1',confirmationDate:'2026-11-03'}]},
                       {...summary,newBuySignals:[{type:'S1',confirmationDate:target}]}])assert.equal(m.isFreshBuy(changed,target),false);
  assert.equal(m.isFreshBuy(null,target),false);
});
test('sort only moves new-B group ahead, keeping input order within both groups', () => {
  const m=monitor();const items=[{symbol:'AAPL'},{symbol:'002156.SZ'},{symbol:'600186.SS'},{symbol:'600519.SS'}];
  const summaries={'002156.SZ':run(), '600519.SS':run(payload('600519.SS')),'600186.SS':run(payload('600186.SS'),[])};
  const saved=JSON.stringify({items,summaries});const sorted=m.sortItems(items,summaries,target);
  assert.deepEqual(sorted.map(x=>x.symbol),['002156.SZ','600519.SS','AAPL','600186.SS']);
  assert.notEqual(sorted,items);assert.equal(JSON.stringify({items,summaries}),saved);
  assert.deepEqual(m.sortItems(items,summaries,'2026-11-05'),items);assert.deepEqual(m.sortItems(items,summaries,target,false),items);
  assert.deepEqual(m.sortItems(items,{'600519.SS':summaries['002156.SZ']},target),items);
});
test('insufficient bars can show verified price but no analysis or buy status', () => {
  const p=payload();p.bars=p.bars.slice(-4);let called=false;
  const result=monitor().analyzeHistory(p.meta.symbol,p,target,()=>{called=true;return analysis()();});
  assert.equal(called,false);assert.equal(result.closeDate,target);assert.equal(result.status,'insufficient_history');assert.equal(result.ready,false);
});
test('engine failure is explicit and does not discard a valid close', () => {
  const result=monitor().analyzeHistory('002156.SZ',payload(),target,()=>{throw Error('test failure');});
  assert.equal(result.status,'analysis_error');assert.equal(result.close,11);assert.equal(result.isNewBuy,false);assert.match(result.message,/test failure/);
});
test('malformed signals are rejected instead of fabricating confirmation dates', () => {
  for(const broken of [{index:8},{index:-1},{index:1.1},{pivotIndex:8},{pivotIndex:-1},{type:'B9'},{price:NaN},{stop:null}]){
    const result=run(payload(),[signal('B1',7,broken)]);assert.equal(result.status,'analysis_error');assert.deepEqual(result.newBuySignals,[]);
  }
});
test('at most two calendar years are supplied and engine cannot mutate original input', () => {
  const p=payload();p.bars.unshift({...p.bars[0],time:'2023-01-01'},{...p.bars[0],time:'2024-11-03'},{...p.bars[0],time:'2024-11-04'});
  const saved=JSON.stringify(p);let received;
  const result=monitor().analyzeHistory(p.meta.symbol,p,target,bars=>{received=bars.map(b=>({...b}));bars[0].close=999;return {signals:[],ruleVersion:'test'};});
  assert.equal(received[0].time,'2024-11-04');assert.equal(JSON.stringify(p),saved);assert.equal(result.close,11);
});
test('real saved online inputs: 002156 target B versus 600186 historical B', () => {
  const engine=require('./engine.js');const m=monitor();
  const a=JSON.parse(fs.readFileSync(path.join(__dirname,'verification/watch-monitor-20261105/baseline-002156.SZ.json'),'utf8'));
  const b=JSON.parse(fs.readFileSync(path.join(__dirname,'verification/watch-monitor-20261105/baseline-600186.SS.json'),'utf8'));
  const sa=m.analyzeHistory('002156.SZ',a,target,engine.analyze),sb=m.analyzeHistory('600186.SS',b,target,engine.analyze);
  assert.equal(sa.isNewBuy,true);assert.equal(sa.close,a.bars.at(-1).close);assert.equal(sa.close.toFixed(2),'42.33');assert.equal(sa.latestSignalDate,target);
  assert.equal(sb.isNewBuy,false);assert.equal(sb.close,b.bars.at(-1).close);assert.equal(sb.close.toFixed(2),'8.58');assert.equal(sb.latestSignalDate,'2026-10-27');
});
