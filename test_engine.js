const assert = require('node:assert/strict');
const fs = require('node:fs');
const E = require('./engine.js');

async function main() {
  const bars = E.sampleData(240);
  const full = E.analyze(bars);
  for (let n = 5; n <= bars.length; n++) {
    assert.deepEqual(E.analyze(bars.slice(0, n)).signals, full.signals.filter(s => s.index < n), `frozen prefix ${n}`);
  }
  assert.equal(new Set(full.signals.map(s => s.id)).size, full.signals.length);
  const online = await fetch('http://127.0.0.1:8765/api/history?symbol=510300.SS&range=2y').then(r => { assert(r.ok); return r.json(); });
  const result = E.backtest(online.bars);
  const signals = new Map(result.signals.map(s => [s.id, s]));
  for (const t of result.trades) {
    if (t.side === 'BUY') assert.equal(t.index, signals.get(t.signalId).index + 1, 'next bar entry');
    else assert(online.bars[t.exitIndex].time.slice(0, 10) > online.bars[t.entryIndex].time.slice(0, 10), 'T+1 selling');
  }
  for (const s of result.signals.filter(s => /[BS]3/.test(s.type))) {
    assert(s.breakIndex > s.center.confirmedAt);
    assert(s.pivotIndex > s.breakIndex);
    if (s.type === 'B3') {
      assert(online.bars[s.breakIndex].close > s.center.ZG);
      assert(s.price > s.center.ZG);
    } else {
      assert(online.bars[s.breakIndex].close < s.center.ZD);
      assert(s.price < s.center.ZD);
    }
  }
  const record = { testedAt: new Date().toISOString(), ruleVersion: result.analysis.ruleVersion, checks: ['all-prefix frozen signal equality', 'unique IDs', 'next bar entries', 'T+1 exits', 'third point break then pivot'], data: { ...online.meta, bars: online.bars.length }, metrics: result.metrics };
  fs.writeFileSync('validation.json', JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
}
main().catch(e => { console.error(e); process.exitCode = 1; });
