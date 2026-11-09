'use strict';
// NDJSON bridge for daily scanning. It loads the SAME engine as the web app;
// it does not reimplement, predict, or backfill Chan buy points.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { once } = require('node:events');
const enginePath = path.join(__dirname, 'engine.js');
const engineSha256 = crypto.createHash('sha256').update(fs.readFileSync(enginePath)).digest('hex').toUpperCase();
const E = require(enginePath);
const RULE_VERSION = 'chan-proxy-0.2-causal-events';
const BUY_TYPES = new Set(['B1', 'B2', 'B3']);
const SELL_TYPES = new Set(['S1', 'S2', 'S3']);
const finiteOrNull = n => Number.isFinite(n) ? n : null;

function summarizeSignals(analysis, bars, targetDate) {
  const target = bars.find(b => b.time === targetDate);
  if (!target) return [];
  const current = analysis.signals.filter(s => bars[s.index]?.time === targetDate);
  const concurrentSell = current.some(s => SELL_TYPES.has(s.type));
  return current.filter(s => BUY_TYPES.has(s.type)).map(s => ({
    id: s.id,
    type: s.type,
    confirmationDate: bars[s.index].time,
    pivotDate: bars[s.pivotIndex]?.time || null,
    rawIndex: s.index,
    rawPivotIndex: s.pivotIndex,
    price: finiteOrNull(s.price),
    stop: finiteOrNull(s.stop),
    reason: s.reason || '',
    center: s.center ? structuredClone(s.center) : null,
    centerId: s.centerId || null,
    close: target.close,
    // A numeric percentage: 5 means 5%, denominator is target-day close.
    distanceToStopPct: Number.isFinite(s.stop) ? (target.close - s.stop) / target.close * 100 : null,
    distanceToStopUnit: 'percent_of_target_close',
    invalidatedAtClose: Number.isFinite(s.stop) ? target.close <= s.stop : null,
    concurrentSell,
    breakDate: bars[s.breakIndex]?.time || null,
    firstSignalId: s.firstSignalId || null,
    legPivotDate: bars[s.legPivotIndex]?.time || null,
    experimental: true
  }));
}

function analyzeOne(request) {
  const response = {
    code: typeof request?.code === 'string' ? request.code : null,
    name: typeof request?.name === 'string' ? request.name : null,
    symbol: typeof request?.symbol === 'string' ? request.symbol : null,
    targetDate: typeof request?.targetDate === 'string' ? request.targetDate : null,
    status: 'error', asOf: null, barCount: 0, signals: [],
    ruleVersion: RULE_VERSION, engineSha256
  };
  try {
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('request must be an object');
    if (typeof request.targetDate !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(request.targetDate)) throw new Error('targetDate must be YYYY-MM-DD');
    const targetDate = E.normalizeTradingDay(request.targetDate);
    if (!Array.isArray(request.bars)) throw new Error('bars must be an array');
    let previous = null;
    const bars = request.bars.map((b, i) => {
      if (!b || typeof b !== 'object') throw new Error(`bar ${i + 1} must be an object`);
      const time = E.normalizeTradingDay(b.time, i + 1);
      if (previous !== null && time <= previous) throw new Error('bar dates must be strictly increasing');
      previous = time;
      return { ...b, time };
    }).filter(b => b.time <= targetDate);
    response.barCount = bars.length;
    response.asOf = bars.at(-1)?.time || null;
    if (!bars.length) return { ...response, status: 'insufficient', reason: 'no bars at or before targetDate' };
    if (response.asOf !== targetDate) return { ...response, status: 'stale', reason: 'target trading day is absent; no historical signal reused' };
    if (bars.length < 5) return { ...response, status: 'insufficient', reason: 'engine requires at least 5 bars' };
    const analysis = E.analyze(bars);
    response.ruleVersion = analysis.ruleVersion || RULE_VERSION;
    response.signals = summarizeSignals(analysis, bars, targetDate);
    response.status = 'ok';
    response.close = bars.at(-1).close;
    return response;
  } catch (error) {
    return { ...response, status: 'error', signals: [], error: error.message };
  }
}

async function main() {
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let lineNumber = 0;
  for await (const line of input) {
    lineNumber++;
    if (!line.trim()) continue;
    let output;
    try {
      output = analyzeOne(JSON.parse(line.replace(/^\uFEFF/, '')));
    } catch (error) {
      output = { status: 'error', lineNumber, error: `Invalid JSON: ${error.message}`, asOf: null, barCount: 0, signals: [], ruleVersion: RULE_VERSION, engineSha256 };
    }
    if (!process.stdout.write(JSON.stringify(output) + '\n')) await once(process.stdout, 'drain');
  }
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { analyzeOne, summarizeSignals, engineSha256 };
