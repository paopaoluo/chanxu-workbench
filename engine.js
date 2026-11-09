/* Chan theory research engine. No live data, orders, or investment advice. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ChanEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DEFAULTS = {
    initialCapital: 100000,
    feeBps: 3,
    slippageBps: 5,
    positionPct: 20,
    riskPct: 1,
    lotSize: 100,
    tPlusOne: true,
    signalTypes: ['B1', 'B2', 'B3'],
    macd: { fast: 12, slow: 26, signal: 9 }
  };

  function fail(message, row, field) {
    const suffix = row == null ? '' : `（第 ${row} 行）`;
    const error = new Error(message + suffix);
    if (row != null) error.row = row;
    if (field) error.field = field;
    throw error;
  }

  function num(value, field, row) {
    if (value == null || String(value).trim() === '') fail(`${field} 不能为空`, row, field);
    const n = Number(String(value).trim());
    if (!Number.isFinite(n)) fail(`${field} 必须是有限数值`, row, field);
    return n;
  }

  // Daily bars use a calendar date, never a locale-dependent Date.parse result.
  // Accepted timestamps require an explicit timezone and retain the calendar
  // date written in that timezone; they are not shifted to a UTC trading day.
  function normalizeTime(value, row) {
    if (typeof value !== 'string' || !value.trim()) fail('time 日期不能为空', row, 'time');
    const text = value.trim();
    let parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    let year, month, day;
    if (parts) [, year, month, day] = parts;
    else if ((parts = /^(\d{4})\/(\d{1,2})\/(\d{1,2})$/.exec(text))) [, year, month, day] = parts;
    else if ((parts = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(text))) [, month, day, year] = parts;
    else if ((parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(text))) {
      [, year, month, day] = parts;
      const hour = Number(parts[4]), minute = Number(parts[5]), second = Number(parts[6]);
      const offsetHour = Number(parts[10] || 0), offsetMinute = Number(parts[11] || 0);
      if (hour > 23 || minute > 59 || second > 59 || offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)) fail('time 时间或时区偏移无效', row, 'time');
    } else fail('time 格式不支持，请使用 YYYY-MM-DD、YYYY/M/D、M/D/YYYY，或带时区的 ISO 时间戳', row, 'time');
    year = Number(year); month = Number(month); day = Number(day);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > monthDays[month - 1]) fail('time 不是有效的日历日期', row, 'time');
    return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }

  function parseCSV(text) {
    if (typeof text !== 'string' || !text.trim()) fail('CSV 文件为空');
    const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).map((text, index) => ({ text, row: index + 1 })).filter(line => line.text.trim());
    const split = line => line.split(',').map(v => v.trim());
    const first = split(lines[0].text).map(v => v.toLowerCase());
    const hasHeader = ['time', 'date', 'datetime', 'timestamp'].some(k => first.includes(k));
    const header = hasHeader ? first : ['time', 'open', 'high', 'low', 'close', 'volume'];
    const idx = name => {
      const aliases = { time: ['time', 'date', 'datetime', 'timestamp'], open: ['open', 'o'], high: ['high', 'h'], low: ['low', 'l'], close: ['close', 'c'], volume: ['volume', 'vol', 'v'] };
      const found = aliases[name].map(x => header.indexOf(x)).find(x => x >= 0);
      if (found == null) fail(`CSV 缺少 ${name} 列`, lines[0].row, name);
      return found;
    };
    const cols = { time: idx('time'), open: idx('open'), high: idx('high'), low: idx('low'), close: idx('close'), volume: idx('volume') };
    const start = hasHeader ? 1 : 0;
    const bars = [];
    let previousTime = null;
    for (let r = start; r < lines.length; r += 1) {
      const row = lines[r].row, values = split(lines[r].text);
      if (values.length < header.length) fail('CSV 数据列数不足', row);
      const time = normalizeTime(values[cols.time], row);
      const open = num(values[cols.open], 'open', row);
      const high = num(values[cols.high], 'high', row);
      const low = num(values[cols.low], 'low', row);
      const close = num(values[cols.close], 'close', row);
      const volume = num(values[cols.volume], 'volume', row);
      if (!(open > 0 && high > 0 && low > 0 && close > 0 && volume >= 0)) fail('OHLC 价格必须为正，volume 成交量不能为负', row);
      if (high < Math.max(open, close, low) || low > Math.min(open, close, high)) fail('OHLC 高低价与开收盘价关系无效', row);
      if (previousTime && time <= previousTime) fail('time 日期必须严格递增，不支持重复日期或倒序数据', row, 'time');
      previousTime = time;
      bars.push({ time, open, high, low, close, volume });
    }
    if (bars.length < 5) fail('至少需要 5 根 K 线');
    return bars;
  }

  function sampleData(count = 240) {
    if (!Number.isSafeInteger(count) || count < 0 || count > 2913174) fail('示例 K 线数量必须是有效的非负整数', null, 'count');
    const out = [];
    let close = 98;
    for (let i = 0; i < count; i += 1) {
      const wave = Math.sin(i / 5.3) * 2.2 + Math.sin(i / 13.1) * 3.4;
      const regime = i < 55 ? -0.12 * i : i < 110 ? 0.20 * (i - 55) : i < 165 ? -0.16 * (i - 110) : 0.22 * (i - 165);
      const next = 100 + regime + wave + (i % 17 === 0 ? 1.8 : 0);
      const open = close;
      close = Math.max(8, next);
      const high = Math.max(open, close) + 0.8 + Math.abs(Math.sin(i * 1.7)) * 0.7;
      const low = Math.min(open, close) - 0.8 - Math.abs(Math.cos(i * 1.3)) * 0.7;
      // Deterministic synthetic daily data, including weekends; not a market calendar.
      const time = new Date(Date.UTC(2024, 0, 1) + i * 86400000).toISOString().slice(0, 10);
      out.push({ time, open: +open.toFixed(3), high: +high.toFixed(3), low: +Math.max(1, low).toFixed(3), close: +close.toFixed(3), volume: Math.round(100000 + (i * 7919) % 70000) });
    }
    return out;
  }

  function mergeBars(bars) {
    const merged = [];
    let direction = 0;
    bars.forEach((bar, index) => {
      const current = { ...bar, startIndex: index, endIndex: index, sourceIndices: [index], mergedIndex: merged.length };
      if (!merged.length) { merged.push(current); return; }
      const last = merged[merged.length - 1];
      const contains = (last.high >= current.high && last.low <= current.low) || (current.high >= last.high && current.low <= last.low);
      if (!contains) {
        direction = current.high > last.high || current.low > last.low ? 1 : -1;
        current.mergedIndex = merged.length;
        merged.push(current);
        return;
      }
      if (!direction) direction = current.close >= last.close ? 1 : -1;
      if (direction >= 0) {
        last.high = Math.max(last.high, current.high);
        last.low = Math.max(last.low, current.low);
      } else {
        last.high = Math.min(last.high, current.high);
        last.low = Math.min(last.low, current.low);
      }
      last.close = current.close;
      last.time = current.time;
      last.volume += current.volume;
      last.endIndex = index;
      last.sourceIndices.push(index);
    });
    merged.forEach((b, i) => { b.mergedIndex = i; });
    return merged;
  }

  function fractals(merged) {
    const out = [];
    for (let i = 1; i < merged.length - 1; i += 1) {
      const a = merged[i - 1]; const b = merged[i]; const c = merged[i + 1];
      if (b.high > a.high && b.high > c.high && b.low > a.low && b.low > c.low) out.push({ index: b.endIndex, mergedIndex: i, price: b.high, type: 'top', confirmedAt: c.endIndex });
      else if (b.low < a.low && b.low < c.low && b.high < a.high && b.high < c.high) out.push({ index: b.endIndex, mergedIndex: i, price: b.low, type: 'bottom', confirmedAt: c.endIndex });
    }
    return out;
  }

  function buildPens(fr) {
    const endpoints = [];
    fr.forEach(f => {
      if (!endpoints.length) { endpoints.push(f); return; }
      const last = endpoints[endpoints.length - 1];
      if (f.type === last.type) { if (f.type === 'top' ? f.price > last.price : f.price < last.price) endpoints[endpoints.length - 1] = f; return; }
      if (f.mergedIndex - last.mergedIndex >= 4 && (f.type === 'top' ? f.price > last.price : f.price < last.price)) endpoints.push(f);
    });
    const pens = [];
    for (let i = 1; i < endpoints.length; i += 1) {
      const a = endpoints[i - 1]; const b = endpoints[i];
      pens.push({ start: { index: a.index, price: a.price }, end: { index: b.index, price: b.price }, startMergedIndex: a.mergedIndex, endMergedIndex: b.mergedIndex, direction: b.type === 'top' ? 'up' : 'down', confirmedAt: b.confirmedAt });
    }
    return pens;
  }

  function buildSegments(pens) {
    const segments = [];
    for (let i = 0; i + 2 < pens.length; i += 1) {
      const p = pens.slice(i, i + 3);
      const hi = Math.min(...p.map(x => Math.max(x.start.price, x.end.price)));
      const lo = Math.max(...p.map(x => Math.min(x.start.price, x.end.price)));
      if (hi >= lo) segments.push({ start: p[0].start, end: p[2].end, startMergedIndex: p[0].startMergedIndex, endMergedIndex: p[2].endMergedIndex, direction: p[0].direction, confirmedAt: p[2].confirmedAt, proxy: true, note: 'three-pen proxy; characteristic sequence rules are not implemented' });
    }
    return segments;
  }

  function buildCenters(pens) {
    const centers = [];
    for (let i = 0; i + 2 < pens.length; i += 1) {
      const group = pens.slice(i, i + 3);
      const highs = group.map(p => Math.max(p.start.price, p.end.price));
      const lows = group.map(p => Math.min(p.start.price, p.end.price));
      const ZG = Math.min(...highs); const ZD = Math.max(...lows);
      if (ZG > ZD) centers.push({ id: `C${centers.length + 1}`, startIndex: group[0].start.index, endIndex: group[2].end.index, ZG, ZD, confirmedAt: group[2].confirmedAt, level: 'proxy', definition: 'three consecutive pens overlap; not strict recursive center' });
    }
    return centers;
  }

  function macd(bars, cfg) {
    const fast = cfg.fast; const slow = cfg.slow; const signal = cfg.signal;
    let ef = bars[0].close; let es = bars[0].close; let sig = 0;
    const af = 2 / (fast + 1); const as = 2 / (slow + 1); const ag = 2 / (signal + 1);
    return bars.map((b, i) => { if (i) { ef = ef + af * (b.close - ef); es = es + as * (b.close - es); } const line = ef - es; sig = i ? sig + ag * (line - sig) : line; return { index: i, line, signal: sig, histogram: line - sig }; });
  }

  function snapshot(bars, options = {}) {
    if (!Array.isArray(bars) || bars.length < 5) fail('分析至少需要 5 根 K 线');
    const merged = mergeBars(bars); const fs = fractals(merged); const pens = buildPens(fs); const segments = buildSegments(pens); const centers = buildCenters(pens); const md = macd(bars, { ...DEFAULTS.macd, ...(options.macd || {}) });
    const signals = []; let b1 = null; let s1 = null; let id = 1;
    const centerFor = (rawIndex) => centers.filter(c => c.confirmedAt <= rawIndex).slice(-1)[0];
    fs.forEach(f => {
      const m = md[f.index] || md[md.length - 1];
      const previous = fs.filter(x => x.type === f.type && x.mergedIndex < f.mergedIndex).slice(-1)[0];
      const prevMd = previous ? md[previous.index] : null;
      if (f.type === 'bottom' && previous && f.price < previous.price && prevMd && Math.abs(m.histogram) < Math.abs(prevMd.histogram) && centerFor(f.confirmedAt)) {
        const signal = { id: `SIG${id++}`, type: 'B1', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: f.price, reason: '实验一买：下跌创新低且 MACD 柱体绝对值减弱，并已有代理中枢', centerId: centerFor(f.confirmedAt).id }; signals.push(signal); b1 = signal;
      } else if (f.type === 'top' && previous && f.price > previous.price && prevMd && Math.abs(m.histogram) < Math.abs(prevMd.histogram) && centerFor(f.confirmedAt)) {
        const signal = { id: `SIG${id++}`, type: 'S1', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: f.price, reason: '实验一卖：上涨创新高且 MACD 柱体绝对值减弱，并已有代理中枢', centerId: centerFor(f.confirmedAt).id }; signals.push(signal); s1 = signal;
      } else if (f.type === 'bottom' && b1 && f.price > b1.price && f.confirmedAt > b1.index) {
        signals.push({ id: `SIG${id++}`, type: 'B2', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: b1.stop, reason: '实验二买：一买后回调低点高于一买低点', centerId: b1.centerId }); b1 = null;
      } else if (f.type === 'top' && s1 && f.price < s1.price && f.confirmedAt > s1.index) {
        signals.push({ id: `SIG${id++}`, type: 'S2', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: s1.stop, reason: '实验二卖：一卖后反弹高点低于一卖高点', centerId: s1.centerId }); s1 = null;
      }
      const c = centers.filter(x => x.confirmedAt < f.confirmedAt).slice(-1)[0];
      if (c && f.type === 'bottom' && f.price > c.ZG && f.confirmedAt > c.endIndex) signals.push({ id: `SIG${id++}`, type: 'B3', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: c.ZG, reason: '实验三买：回抽低点保持在代理中枢上沿之上', centerId: c.id });
      if (c && f.type === 'top' && f.price < c.ZD && f.confirmedAt > c.endIndex) signals.push({ id: `SIG${id++}`, type: 'S3', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: c.ZD, reason: '实验三卖：回抽高点保持在代理中枢下沿之下', centerId: c.id });
    });
    signals.sort((a, b) => a.index - b.index || a.id.localeCompare(b.id));
    return { merged, fractals: fs, pens, segments, centers, signals, macd: md };
  }

  function analyze(bars, options = {}) {
    bars = validateBars(bars);
    const opts = validateOptions(options);
    const result = snapshot(bars, opts);
    const signals = []; const seen = new Set(); const frozenCenters = new Map();
    const centerStates = new Map();
    let pendingB1 = null;
    let pendingS1 = null;
    let frozenId = 1;
    const MAX_PENDING_BARS = 100;
    // A three-pen proxy whose first endpoint is unchanged is the same candidate
    // even if a later extreme redraws its third endpoint. Freeze the first version.
    const centerKey = c => String(c.startIndex);
    const emit = (signal, center) => {
      const key = `${signal.type}-${signal.pivotIndex}`;
      if (seen.has(key)) return null;
      seen.add(key);
      const copy = { ...signal, id: key, confirmedAt: signal.index, center: center ? { ...center } : null, centerId: center ? center.id : signal.centerId, experimental: true };
      signals.push(copy);
      return copy;
    };
    for (let end = 5; end <= bars.length; end += 1) {
      const prefix = snapshot(bars.slice(0, end), opts);
      const currentIndex = end - 1;
      const bar = bars[currentIndex];

      // A center is frozen at the first prefix in which its proxy overlap is confirmed.
      prefix.centers.filter(c => c.confirmedAt === currentIndex).forEach(c => {
        const key = centerKey(c);
        if (frozenCenters.has(key)) return;
        const frozen = { ...c, id: `FC${frozenId++}`, frozenAt: currentIndex, sourceId: c.id };
        frozenCenters.set(key, frozen);
        centerStates.set(frozen.id, { center: frozen, phase: 'armed', breakIndex: null, signalId: null });
      });

      // Existing pending second-buy/second-sell structures are invalidated by a breach,
      // an opposing signal, or a bounded wait. These are persistent events, not rebuilt
      // from the final chart snapshot.
      if (pendingB1 && currentIndex > pendingB1.signal.index) {
        if (bar.low <= pendingB1.signal.price || currentIndex - pendingB1.signal.index > MAX_PENDING_BARS) pendingB1 = null;
      }
      if (pendingS1 && currentIndex > pendingS1.signal.index) {
        if (bar.high >= pendingS1.signal.price || currentIndex - pendingS1.signal.index > MAX_PENDING_BARS) pendingS1 = null;
      }

      // A center gets one break opportunity only. The break must be a later completed
      // bar close; any later return into the center invalidates that opportunity.
      centerStates.forEach(state => {
        const c = state.center;
        if (state.phase === 'armed' && currentIndex > c.confirmedAt && bar.close > c.ZG) {
          state.phase = 'broken-up';
          state.breakIndex = currentIndex;
        } else if (state.phase === 'broken-up' && currentIndex > state.breakIndex && bar.low <= c.ZG) {
          state.phase = 'invalid';
        } else if (state.phase === 'armed' && currentIndex > c.confirmedAt && bar.close < c.ZD) {
          state.phase = 'broken-down';
          state.breakIndex = currentIndex;
        } else if (state.phase === 'broken-down' && currentIndex > state.breakIndex && bar.high >= c.ZD) {
          state.phase = 'invalid';
        }
      });

      const prefixSignals = prefix.signals.filter(s => s.index === currentIndex);
      const s1Event = prefixSignals.find(s => s.type === 'S1');
      const b1Event = prefixSignals.find(s => s.type === 'B1');
      const eventFractals = prefix.fractals.filter(f => f.confirmedAt === currentIndex);

      // A confirmed sell cancels an unconfirmed first-buy setup. The symmetric state
      // prevents an old first-sell from producing a later second-sell after invalidation.
      if (s1Event) pendingB1 = null;
      if (b1Event) pendingS1 = null;

      if (pendingB1) {
        const top = eventFractals.find(f => f.type === 'top' && f.index > pendingB1.signal.index && f.price > pendingB1.signal.price);
        if (pendingB1.phase === 'await-top' && top) {
          pendingB1.phase = 'await-bottom';
          pendingB1.top = top;
        } else if (pendingB1.phase === 'await-bottom') {
          const bottom = eventFractals.find(f => f.type === 'bottom' && f.index > pendingB1.top.confirmedAt && f.price > pendingB1.signal.price && f.price < pendingB1.top.price);
          if (bottom) {
            emit({ type: 'B2', index: bottom.confirmedAt, pivotIndex: bottom.index, price: bottom.price, stop: pendingB1.signal.stop, firstSignalId: pendingB1.signal.id, firstIndex: pendingB1.signal.index, legPivotIndex: pendingB1.top.index, legConfirmedAt: pendingB1.top.confirmedAt, reason: '实验二买：一买后先确认更高顶上涨腿，再确认低点高于一买低点的回调', centerId: pendingB1.signal.centerId }, pendingB1.signal.center);
            pendingB1 = null;
          }
        }
      }
      if (pendingS1) {
        const bottom = eventFractals.find(f => f.type === 'bottom' && f.index > pendingS1.signal.index && f.price < pendingS1.signal.price);
        if (pendingS1.phase === 'await-bottom' && bottom) {
          pendingS1.phase = 'await-top';
          pendingS1.bottom = bottom;
        } else if (pendingS1.phase === 'await-top') {
          const top = eventFractals.find(f => f.type === 'top' && f.index > pendingS1.bottom.confirmedAt && f.price < pendingS1.signal.price && f.price > pendingS1.bottom.price);
          if (top) {
            emit({ type: 'S2', index: top.confirmedAt, pivotIndex: top.index, price: top.price, stop: pendingS1.signal.stop, firstSignalId: pendingS1.signal.id, firstIndex: pendingS1.signal.index, legPivotIndex: pendingS1.bottom.index, legConfirmedAt: pendingS1.bottom.confirmedAt, reason: '实验二卖：一卖后先确认更低底回落腿，再确认高点低于一卖高点的反弹', centerId: pendingS1.signal.centerId }, pendingS1.signal.center);
            pendingS1 = null;
          }
        }
      }

      if (b1Event) {
        const source = prefix.centers.find(x => x.id === b1Event.centerId);
        const frozen = source ? frozenCenters.get(centerKey(source)) : null;
        const signal = emit(b1Event, frozen);
        if (signal) pendingB1 = { signal, phase: 'await-top', top: null };
      }
      if (s1Event) {
        const source = prefix.centers.find(x => x.id === s1Event.centerId);
        const frozen = source ? frozenCenters.get(centerKey(source)) : null;
        const signal = emit(s1Event, frozen);
        if (signal) pendingS1 = { signal, phase: 'await-bottom', bottom: null };
      }

      // B3/S3 only occur after a frozen center has one completed-bar departure and a
      // later fractal pullback that stays outside the frozen center boundary.
      eventFractals.forEach(f => {
        centerStates.forEach(state => {
          const c = state.center;
          if (state.phase === 'broken-up' && f.type === 'bottom' && f.index > state.breakIndex && f.price > c.ZG) {
            const signal = emit({ type: 'B3', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: c.ZG, breakIndex: state.breakIndex, reason: '实验三买：冻结中枢上沿被完成K线收盘离开，后续回撤底分型未回到ZG', centerId: c.id }, c);
            state.phase = 'emitted-up'; state.signalId = signal && signal.id;
          } else if (state.phase === 'broken-down' && f.type === 'top' && f.index > state.breakIndex && f.price < c.ZD) {
            const signal = emit({ type: 'S3', index: f.confirmedAt, pivotIndex: f.index, price: f.price, stop: c.ZD, breakIndex: state.breakIndex, reason: '实验三卖：冻结中枢下沿被完成K线收盘离开，后续回撤顶分型未回到ZD', centerId: c.id }, c);
            state.phase = 'emitted-down'; state.signalId = signal && signal.id;
          }
        });
      });
      if (signals.some(s => s.index === currentIndex && /^S[123]$/.test(s.type))) pendingB1 = null;
      if (signals.some(s => s.index === currentIndex && /^B[123]$/.test(s.type))) pendingS1 = null;
    }
    signals.sort((a, b) => a.index - b.index || a.id.localeCompare(b.id));
    return { ...result, signals, frozenCenters: [...frozenCenters.values()], ruleVersion: 'chan-proxy-0.2-causal-events' };
  }

  function validateBars(bars) {
    if (!Array.isArray(bars) || bars.length < 5) fail('至少需要 5 根 K 线');
    let prior = null;
    return bars.map((b, i) => {
      if (!b) fail('K 线数据为空', i + 1);
      const time = normalizeTime(b.time, i + 1);
      ['open', 'high', 'low', 'close', 'volume'].forEach(k => { if (!Number.isFinite(b[k])) fail(`${k} 必须是有限数值`, i + 1, k); });
      if (Math.min(b.open, b.high, b.low, b.close) <= 0 || b.volume < 0 || b.high < Math.max(b.open, b.close, b.low) || b.low > Math.min(b.open, b.close, b.high)) fail('OHLCV 价格范围、成交量或高低价关系无效', i + 1);
      if (prior != null && time <= prior) fail('time 日期必须严格递增，不支持重复日期或倒序数据', i + 1, 'time');
      prior = time;
      return { ...b, time };
    });
  }

  function validateOptions(options) {
    const o = { ...DEFAULTS, ...options, macd: { ...DEFAULTS.macd, ...(options.macd || {}) } };
    ['initialCapital', 'feeBps', 'slippageBps', 'positionPct', 'riskPct', 'lotSize'].forEach(k => { if (!(Number.isFinite(o[k]) && o[k] >= 0)) fail(`${k} 必须是非负有限数值`, null, k); });
    if (!(o.initialCapital > 0)) fail('初始资金 initialCapital 必须大于 0', null, 'initialCapital');
    if (!(Number.isInteger(o.lotSize) && o.lotSize >= 1)) fail('交易单位 lotSize 必须为正整数', null, 'lotSize');
    if (!(o.positionPct > 0 && o.positionPct <= 100)) fail('仓位 positionPct 必须大于 0 且不超过 100%', null, 'positionPct');
    if (!(o.riskPct > 0 && o.riskPct <= 100)) fail('风险预算 riskPct 必须大于 0 且不超过 100%', null, 'riskPct');
    if (!(o.feeBps < 10000)) fail('佣金 feeBps 必须小于 10000 bp', null, 'feeBps');
    if (!(o.slippageBps < 10000)) fail('滑点 slippageBps 必须小于 10000 bp', null, 'slippageBps');
    if (typeof o.tPlusOne !== 'boolean' || !Array.isArray(o.signalTypes) || o.signalTypes.some(x => !['B1', 'B2', 'B3'].includes(x))) fail('T+1 或买点类型参数无效');
    if (!['fast', 'slow', 'signal'].every(k => Number.isInteger(o.macd[k]) && o.macd[k] > 0) || o.macd.fast >= o.macd.slow) fail('MACD 周期必须为正整数，且快线周期小于慢线周期', null, 'macd');
    return o;
  }

  function backtest(bars, options = {}) {
    bars = validateBars(bars);
    const o = validateOptions(options); const result = analyze(bars, o); const buyTypes = new Set((o.signalTypes || []).filter(t => /^B[123]$/.test(t))); const byIndex = new Map();
    result.signals.forEach(s => { if (buyTypes.has(s.type) || /^S[123]$/.test(s.type)) { const key = s.index + 1; if (key < bars.length) { const prior = byIndex.get(key) || []; prior.push(s); byIndex.set(key, prior); } } });
    let cash = o.initialCapital; let position = null; let fees = 0; const trades = []; const equity = []; const exposure = [];
    const fee = value => value * o.feeBps / 10000;
    function sell(index, price, reason, signal) { if (!position) return; const gross = position.qty * price; const f = fee(gross); cash += gross - f; fees += f; const pnl = (price - position.entryPrice) * position.qty - position.entryFees - f; trades.push({ side: 'SELL', entryIndex: position.entryIndex, exitIndex: index, entryPrice: position.entryPrice, exitPrice: price, qty: position.qty, pnl, reason, signalId: signal && signal.id, fees: position.entryFees + f }); position = null; }
    for (let i = 0; i < bars.length; i += 1) {
      const bar = bars[i]; const daySignals = byIndex.get(i) || []; const sellSignal = daySignals.find(s => /^S[123]$/.test(s.type));
      let exited = false;
      const canSell = () => position && (!o.tPlusOne || bar.time > bars[position.entryIndex].time);
      if (position && (sellSignal || position.pendingExit)) {
        if (canSell()) { sell(i, bar.open * (1 - o.slippageBps / 10000), position.pendingExit || 'sell-signal', sellSignal); exited = true; }
        else position.pendingExit = position.pendingExit || 'deferred-sell-signal';
      }
      if (position && bar.low <= position.stop) { if (canSell()) { const stopPrice = bar.open <= position.stop ? bar.open : position.stop; sell(i, stopPrice * (1 - o.slippageBps / 10000), 'structure-stop', null); exited = true; } else position.pendingExit = 'deferred-structure-stop'; }
      if (!position && !sellSignal && !exited) {
        const buySignal = daySignals.find(s => buyTypes.has(s.type));
        if (buySignal) {
          const px = bar.open * (1 + o.slippageBps / 10000); const budget = cash * o.positionPct / 100;
          const lossPerUnit = px - buySignal.stop + px * (o.feeBps * 2 + o.slippageBps) / 10000;
          const qty = px > buySignal.stop ? Math.floor(Math.min(budget / px, cash * o.riskPct / 100 / lossPerUnit, cash / (px * (1 + o.feeBps / 10000))) / o.lotSize) * o.lotSize : 0;
          if (qty >= o.lotSize) { const gross = qty * px; const f = fee(gross); if (gross + f <= cash) { cash -= gross + f; fees += f; position = { qty, entryPrice: px, entryIndex: i, entryFees: f, stop: buySignal.stop, signalId: buySignal.id }; trades.push({ side: 'BUY', index: i, price: px, qty, signalId: buySignal.id, fees: f });
            if (bar.low <= position.stop) { if (!o.tPlusOne) { sell(i, Math.min(bar.open, position.stop) * (1 - o.slippageBps / 10000), 'same-bar-conservative-stop', null); } else position.pendingExit = 'deferred-structure-stop'; }
          } }
        }
      }
      const value = cash + (position ? position.qty * bar.close : 0); equity.push({ index: i, time: bar.time, value }); exposure.push(position ? (position.qty * bar.close) / value : 0);
    }
    const final = equity[equity.length - 1].value; let peak = equity[0].value; let maxDd = 0; equity.forEach(e => { peak = Math.max(peak, e.value); maxDd = Math.max(maxDd, (peak - e.value) / peak); });
    const roundTrips = trades.filter(t => t.side === 'SELL'); const wins = roundTrips.filter(t => t.pnl > 0); const gains = roundTrips.filter(t => t.pnl > 0).reduce((a, t) => a + t.pnl, 0); const losses = -roundTrips.filter(t => t.pnl < 0).reduce((a, t) => a + t.pnl, 0);
    return { trades, equity, metrics: { totalReturn: final / o.initialCapital - 1, maxDrawdown: maxDd, winRate: roundTrips.length ? wins.length / roundTrips.length : 0, profitFactor: losses ? gains / losses : gains ? Infinity : 0, tradeCount: roundTrips.length, totalFees: fees, finalEquity: final, exposure: exposure.reduce((a, x) => a + x, 0) / exposure.length }, openPosition: position, signals: result.signals, analysis: result };
  }

  return { parseCSV, sampleData, analyze, backtest, normalizeTradingDay: normalizeTime, DEFAULTS };
});
