/* Offline regressions. Synthetic data only; no service, files, or orders. */
const assert = require('node:assert/strict');
const E = require('./engine.js');
const checks = [];
function check(name, fn) { fn(); checks.push(name); }
function csv(bars) { return 'time,open,high,low,close,volume\n' + bars.map(b => [b.time, b.open, b.high, b.low, b.close, b.volume].join(',')).join('\n'); }
function redate(bars, fn) { return bars.map((b, i) => ({ ...b, time: fn(b.time, i) })); }
// Preserve the original audit trigger: first entry Jan 9, next-day exit Jan 10.
const bars = redate(E.sampleData(240), (_, i) => new Date(Date.UTC(2024, 0, 9 - 87 + i)).toISOString().slice(0, 10));
const expected = E.backtest(bars);

check('same calendar dates in CSV/raw inputs produce identical analysis and trades', () => {
  const forms = [
    t => t,
    t => { const [y, m, d] = t.split('-'); return `${Number(m)}/${Number(d)}/${y}`; },
    t => { const [y, m, d] = t.split('-'); return `${y}/${Number(m)}/${Number(d)}`; },
    t => `${t}T00:00:00Z`,
    t => `${t}T00:30:00.125+08:00`,
    t => `${t}T23:59:59-05:00`
  ];
  const analysis = E.analyze(bars);
  for (const format of forms) {
    const source = redate(bars, format), saved = structuredClone(source);
    source.forEach(Object.freeze); Object.freeze(source);
    assert.deepEqual(E.parseCSV(csv(source)), bars);
    assert.deepEqual(E.analyze(source), analysis);
    assert.deepEqual(E.backtest(source), expected);
    assert.deepEqual(source, saved, 'callers retain original objects and strings');
  }
  assert.equal(expected.trades[0].index, 87);
  assert.equal(expected.trades[1].exitIndex, 88, 'Jan 10 must be sellable after Jan 9');
});

check('invalid formats, impossible calendar dates and invalid ISO times are rejected at every entry', () => {
  const invalid = ['not-a-date', '2024-02-30', '2023-02-29', '1900-02-29', '0000-01-01', '2024-13-01', '2024-00-01', '2024-01-00', '2024-04-31', '2024-1-01', '31/01/2024', '2024-01-01T00:00:00', '2024-01-01T24:00:00Z', '2024-01-01T12:60:00Z', '2024-01-01T12:00:60Z', '2024-01-01T00:00:00+14:01', '2024-01-01T00:00:00+15:00', '2024-01-01T00:00:00+08:60'];
  for (const time of invalid) {
    const bad = redate(bars.slice(0, 5), (t, i) => i ? t : time);
    const errorAt = row => error => error.field === 'time' && error.row === row && /time/.test(error.message) && /第 .* 行/.test(error.message);
    assert.throws(() => E.parseCSV(csv(bad)), errorAt(2), time);
    assert.throws(() => E.analyze(bad), errorAt(1), time);
    assert.throws(() => E.backtest(bad), errorAt(1), time);
  }
  const blankLineCSV = csv(redate(bars.slice(0, 5), (t, i) => i ? t : 'bad')).replace('\n', '\n\n');
  assert.throws(() => E.parseCSV(blankLineCSV), error => error.row === 3, 'physical CSV row includes blank lines');
});

check('cross-month/year and leap-day dates normalize correctly', () => {
  for (const dates of [
    ['2023-12-30', '12/31/2023', '2024/1/1', '2024-01-02T23:00:00-05:00', '2024-01-03'],
    ['2024-01-30', '1/31/2024', '2024/2/1', '2024-02-02', '2024-02-03'],
    ['2000-02-27', '2000-02-28', '2/29/2000', '2000-03-01', '2000-03-02']
  ]) {
    const source = redate(bars.slice(0, 5), (_, i) => dates[i]);
    const normalized = E.parseCSV(csv(source));
    assert(normalized.every(b => /^\d{4}-\d{2}-\d{2}$/.test(b.time)));
    assert.doesNotThrow(() => E.analyze(source));
    assert.doesNotThrow(() => E.backtest(source));
  }
});

check('semantic duplicate dates, duplicate timestamp days and reversed input are rejected', () => {
  for (const dates of [
    ['2024-01-01', '1/1/2024', '2024-01-03', '2024-01-04', '2024-01-05'],
    ['2024-01-01T01:00:00Z', '2024-01-01T12:00:00Z', '2024-01-03', '2024-01-04', '2024-01-05'],
    ['2024-01-02', '2024-01-01', '2024-01-03', '2024-01-04', '2024-01-05']
  ]) {
    const source = redate(bars.slice(0, 5), (_, i) => dates[i]);
    assert.throws(() => E.parseCSV(csv(source)), error => error.field === 'time' && /严格递增/.test(error.message));
    assert.throws(() => E.analyze(source), /严格递增/);
    assert.throws(() => E.backtest(source), /严格递增/);
  }
});

check('sample generator emits valid, consecutive calendar dates beyond 336 bars', () => {
  const sample = E.sampleData(1200);
  for (let i = 0; i < sample.length; i++) {
    assert(/^\d{4}-\d{2}-\d{2}$/.test(sample[i].time));
    assert(Number.isFinite(Date.parse(sample[i].time)));
    if (i) assert.equal(Date.parse(sample[i].time) - Date.parse(sample[i - 1].time), 86400000);
  }
  assert.equal(sample[336].time, '2024-12-02');
  assert.doesNotThrow(() => E.analyze(sample));
});

check('all-prefix frozen signals and unique IDs remain intact', () => {
  const full = E.analyze(bars);
  for (let n = 5; n <= bars.length; n++) assert.deepEqual(E.analyze(bars.slice(0, n)).signals, full.signals.filter(s => s.index < n), `prefix ${n}`);
  assert.equal(new Set(full.signals.map(s => s.id)).size, full.signals.length);
  assert.equal(full.signals.length, 11);
});

check('next-bar entries, ISO T+1 exits and third-point ordering remain intact', () => {
  const signals = new Map(expected.signals.map(s => [s.id, s]));
  for (const trade of expected.trades) {
    if (trade.side === 'BUY') assert.equal(trade.index, signals.get(trade.signalId).index + 1);
    else assert(bars[trade.exitIndex].time > bars[trade.entryIndex].time);
  }
  for (const signal of expected.signals.filter(s => /[BS]3/.test(s.type))) {
    assert(signal.breakIndex > signal.center.confirmedAt);
    assert(signal.pivotIndex > signal.breakIndex);
    if (signal.type === 'B3') { assert(bars[signal.breakIndex].close > signal.center.ZG); assert(signal.price > signal.center.ZG); }
    else { assert(bars[signal.breakIndex].close < signal.center.ZD); assert(signal.price < signal.center.ZD); }
  }
});

check('numeric data and default risk parameter bounds remain enforced with field metadata', () => {
  for (const [options, field] of [[{initialCapital:0},'initialCapital'], [{positionPct:101},'positionPct'], [{riskPct:0},'riskPct'], [{lotSize:0},'lotSize'], [{feeBps:10000},'feeBps'], [{slippageBps:10000},'slippageBps'], [{macd:{fast:26,slow:12}},'macd']]) assert.throws(() => E.backtest(bars, options), error => error.field === field);
  const bad = bars.map(b => ({...b})); bad[2].close = NaN;
  assert.throws(() => E.analyze(bad), error => error.field === 'close' && error.row === 3);
});

console.log(JSON.stringify({ok:true,offline:true,runtime:process.version,checks,ruleVersion:expected.analysis.ruleVersion,synthetic:{bars:bars.length,signals:expected.signals.length,roundTrips:expected.metrics.tradeCount,totalReturn:expected.metrics.totalReturn}}, null, 2));
