'use strict';
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const E = require('./engine.js');
const { analyzeOne, summarizeSignals, engineSha256 } = require('./daily_scan_engine.js');
const checks = [];
function check(name, fn) { fn(); checks.push(name); console.log('PASS ' + name); }
const bars = E.sampleData(240);

check('future bars are cut before the real engine; exactly the confirmation date is selected', () => {
  const full = E.analyze(bars);
  const signal = full.signals.find(s => /^B[123]$/.test(s.type));
  assert(signal);
  const targetDate = bars[signal.index].time;
  const request = { code: '000001', name: '测试合成序列', symbol: '000001.SZ', bars, targetDate };
  const actual = analyzeOne(request);
  assert.equal(actual.status, 'ok');
  assert.equal(actual.barCount, signal.index + 1);
  assert.equal(actual.asOf, targetDate);
  const expected = E.analyze(bars.slice(0, signal.index + 1)).signals.filter(s => /^B[123]$/.test(s.type) && s.index === signal.index);
  assert.deepEqual(actual.signals.map(s => s.id), expected.map(s => s.id));
  assert(actual.signals.every(s => s.confirmationDate === targetDate));
  assert.equal(actual.signals[0].pivotDate, bars[expected[0].pivotIndex].time);
  assert.deepEqual(analyzeOne({ ...request, bars: bars.slice(0, signal.index + 1) }), actual);
});

check('stale input never reuses an old B point as a target-day signal', () => {
  const actual = analyzeOne({ bars: bars.slice(0, 100), targetDate: bars[100].time });
  assert.equal(actual.status, 'stale');
  assert.equal(actual.asOf, bars[99].time);
  assert.deepEqual(actual.signals, []);
  const gap = bars.filter((_, i) => i !== 100);
  assert.equal(analyzeOne({ bars: gap, targetDate: bars[100].time }).status, 'stale');
});

check('insufficient target-day history and malformed input are explicit', () => {
  assert.equal(analyzeOne({ bars: bars.slice(0, 4), targetDate: bars[3].time }).status, 'insufficient');
  assert.equal(analyzeOne({ bars: [], targetDate: bars[3].time }).status, 'insufficient');
  assert.equal(analyzeOne({ bars, targetDate: '2024-02-30' }).status, 'error');
  assert.equal(analyzeOne({ bars, targetDate: '2024/2/1' }).status, 'error');
  assert.equal(analyzeOne({ bars: [...bars.slice(0, 5), bars[4]], targetDate: bars[4].time }).status, 'error');
  assert.equal(analyzeOne({ bars: null, targetDate: bars[4].time }).status, 'error');
});

check('only B1/B2/B3 confirmed on the target day survive, not an earlier pivot date', () => {
  const targetDate = bars[9].time;
  const common = { price: 10, stop: 9, pivotIndex: 7, center: { id: 'frozen', ZD: 8, ZG: 9 } };
  const analysis = { signals: [
    { ...common, id: 'B1-old', type: 'B1', index: 8 },
    { ...common, id: 'B1-now', type: 'B1', index: 9 },
    { ...common, id: 'B2-now', type: 'B2', index: 9 },
    { ...common, id: 'B3-now', type: 'B3', index: 9 },
    { ...common, id: 'S1-now', type: 'S1', index: 9 },
    { ...common, id: 'B9-now', type: 'B9', index: 9 }
  ] };
  const out = summarizeSignals(analysis, bars.slice(0, 10), targetDate);
  assert.deepEqual(out.map(s => s.type), ['B1', 'B2', 'B3']);
  assert(out.every(s => s.concurrentSell));
  assert(out.every(s => s.pivotDate === bars[7].time && s.confirmationDate === targetDate));
  out[0].center.ZG = -99;
  assert.equal(common.center.ZG, 9, 'output center is a deep clone');
});

check('stop distance has explicit percent units and invalidation includes equality', () => {
  const data = bars.slice(0, 10).map(b => ({ ...b })); data[9].close = 10;
  const analysis = { signals: [
    { id: 'B1-9', type: 'B1', index: 9, pivotIndex: 7, price: 9, stop: 9 },
    { id: 'B2-9', type: 'B2', index: 9, pivotIndex: 8, price: 10, stop: 10 },
    { id: 'B3-9', type: 'B3', index: 9, pivotIndex: 8, price: 11, stop: 11 }
  ] };
  const out = summarizeSignals(analysis, data, data[9].time);
  assert.deepEqual(out.map(s => s.distanceToStopPct), [10, 0, -10]);
  assert.deepEqual(out.map(s => s.invalidatedAtClose), [false, true, true]);
  assert(out.every(s => s.distanceToStopUnit === 'percent_of_target_close'));
  assert(out.every(s => s.concurrentSell === false));
});

check('date aliases normalize without mutating caller data', () => {
  const request = { bars: bars.slice(0, 20).map(b => ({ ...b, time: b.time.replaceAll('-', '/') })), targetDate: bars[19].time };
  const original = structuredClone(request);
  assert.equal(analyzeOne(request).status, 'ok');
  assert.deepEqual(request, original);
});

check('NDJSON command keeps errors isolated and emits a result for each nonempty line', () => {
  const valid = { code: '000001', bars: bars.slice(0, 10), targetDate: bars[9].time };
  const run = spawnSync(process.execPath, [path.join(__dirname, 'daily_scan_engine.js')], {
    input: JSON.stringify(valid) + '\n{broken\n' + JSON.stringify(valid) + '\n', encoding: 'utf8'
  });
  assert.equal(run.status, 0, run.stderr);
  assert.equal(run.stderr, '');
  const rows = run.stdout.trim().split('\n').map(JSON.parse);
  assert.deepEqual(rows.map(r => r.status), ['ok', 'error', 'ok']);
  assert.equal(rows[1].lineNumber, 2);
  assert.equal(rows[0].engineSha256, engineSha256);
  assert.equal(rows[0].ruleVersion, 'chan-proxy-0.2-causal-events');
});
console.log(JSON.stringify({ passed: checks.length, checks, engineSha256 }, null, 2));
