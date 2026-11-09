/* Pure watchlist summaries; all signals use the existing injected ChanEngine. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.WatchlistMonitor = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SIGNAL_TYPES = new Set(['B1', 'B2', 'B3', 'S1', 'S2', 'S3']);
  const isBuy = value => value && /^B[123]$/.test(value.type);
  const isCn = symbol => typeof symbol === 'string' && (/^\d{6}\.(SS|SZ)$/.test(symbol) || symbol === '^SSEC');
  const finitePositive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;

  function daysInMonth(year, month) {
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  }
  function validDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split('-').map(Number);
    return year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month);
  }
  function twoYearsBefore(value) {
    const [y, month, day] = value.split('-').map(Number);
    const year = Math.max(1, y - 2);
    return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(Math.min(day, daysInMonth(year, month))).padStart(2, '0')}`;
  }
  function blank(symbol, targetDate) {
    return {symbol, close: null, closeDate: null, currency: '', latestSignalDate: null,
      latestSignals: [], targetDate, targetSignals: [], newBuySignals: [], isNewBuy: false,
      ready: false, status: 'invalid_data', message: '', ruleVersion: null};
  }
  function failure(summary, status, message) {
    summary.ready = false;
    summary.isNewBuy = false;
    summary.latestSignalDate = null;
    summary.latestSignals = [];
    summary.targetSignals = [];
    summary.newBuySignals = [];
    summary.status = status;
    summary.message = message;
    return summary;
  }

  function validateBars(input, cn, targetDate) {
    if (!Array.isArray(input) || input.length === 0) throw Error('没有可用日K数据');
    let previous = null;
    for (const bar of input) {
      if (!bar || !validDate(bar.time)) throw Error('K线日期无效，必须为有效 YYYY-MM-DD');
      if (previous !== null && bar.time <= previous) throw Error('K线日期必须唯一且严格递增');
      previous = bar.time;
    }
    // A future/unfinished CN row is excluded before reading its OHLCV fields.
    let selected = input.filter(bar => !cn || !targetDate || bar.time <= targetDate);
    if (!selected.length) throw Error('目标日之前没有可用日K');
    const cutoff = twoYearsBefore(cn && targetDate ? targetDate : selected[selected.length - 1].time);
    selected = selected.filter(bar => bar.time >= cutoff);
    if (!selected.length) throw Error('最近两年没有可用日K');
    return selected.map(bar => {
      if (!['open', 'high', 'low', 'close'].every(key => finitePositive(bar[key]))
          || typeof bar.volume !== 'number' || !Number.isFinite(bar.volume) || bar.volume < 0) {
        throw Error('OHLC 必须为有限正数，成交量必须为非负有限数；不得缺量或补造数据');
      }
      if (bar.high < Math.max(bar.open, bar.low, bar.close) || bar.low > Math.min(bar.open, bar.high, bar.close)) {
        throw Error('OHLC 高低价与开收盘价不一致');
      }
      return {time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume};
    });
  }

  function normalizeSignal(signal, bars) {
    if (!signal || !SIGNAL_TYPES.has(signal.type)
        || typeof signal.id !== 'string' || !signal.id || signal.id.length > 200
        || !Number.isInteger(signal.index) || signal.index < 0 || signal.index >= bars.length
        || !Number.isInteger(signal.pivotIndex) || signal.pivotIndex < 0 || signal.pivotIndex > signal.index
        || !finitePositive(signal.price) || !finitePositive(signal.stop)) {
      throw Error('引擎信号缺少有效类型、确认索引、极值索引或价格');
    }
    // ID suffix, pivotIndex and any confirmedAt label are not confirmation dates.
    return {id: signal.id, type: signal.type, confirmationDate: bars[signal.index].time,
      pivotDate: bars[signal.pivotIndex].time, price: signal.price, stop: signal.stop};
  }

  function analyzeHistory(rawSymbol, payload, rawTargetDate, analyzeFn) {
    const symbol = typeof rawSymbol === 'string' ? rawSymbol.trim().toUpperCase() : '';
    const targetDate = validDate(rawTargetDate) ? rawTargetDate : null;
    const summary = blank(symbol, targetDate);
    const meta = payload && payload.meta;
    const cn = isCn(symbol);
    let bars;
    try {
      if (!symbol || !meta || typeof meta !== 'object' || meta.symbol !== symbol) throw Error('行情代码与自选标的不一致');
      if (meta.interval !== '1d') throw Error('自选监控仅接受日K数据');
      bars = validateBars(payload.bars, cn, targetDate);
    } catch (error) {
      return failure(summary, 'invalid_data', error && error.message ? error.message : '日K数据无效');
    }
    const last = bars[bars.length - 1];
    summary.close = last.close;
    summary.closeDate = last.time;
    summary.currency = typeof meta.currency === 'string' && /^[A-Z]{3}$/.test(meta.currency) ? meta.currency : '';
    if (bars.length < 5) return failure(summary, 'insufficient_history', '有效日K不足5根；显示实际收盘，暂不计算信号');

    let signals;
    try {
      if (typeof analyzeFn !== 'function') throw Error('缠论引擎不可用');
      // Isolate the caller's payload, and retain independent bars for index mapping.
      const result = analyzeFn(bars.map(bar => ({...bar})));
      if (!result || !Array.isArray(result.signals)) throw Error('引擎没有返回有效信号数组');
      signals = result.signals.map(signal => normalizeSignal(signal, bars));
      summary.ruleVersion = typeof result.ruleVersion === 'string' ? result.ruleVersion : null;
    } catch (error) {
      const message = error && typeof error.message === 'string' ? error.message.slice(0, 180) : '未知错误';
      return failure(summary, 'analysis_error', `信号计算失败：${message}`);
    }
    if (signals.length) {
      summary.latestSignalDate = signals.reduce((latest, signal) => signal.confirmationDate > latest ? signal.confirmationDate : latest, '');
      summary.latestSignals = signals.filter(signal => signal.confirmationDate === summary.latestSignalDate);
    }
    summary.targetSignals = targetDate ? signals.filter(signal => signal.confirmationDate === targetDate) : [];
    summary.ready = cn && !!targetDate && last.time === targetDate && meta.readiness === 'ready'
      && meta.stale === false && meta.completenessPolicy === 'cn_close_v1'
      && meta.expectedThrough === targetDate && meta.asOf === last.time
      && (meta.completeThrough === undefined || meta.completeThrough === last.time);
    if (!cn) {
      summary.status = 'foreign';
      summary.message = '非A股标的：展示实际收盘与历史信号，不按A股目标日高亮置顶';
      if (meta.stale) summary.message += '；当前为旧缓存';
    } else if (!targetDate) {
      summary.status = 'calendar_unavailable';
      summary.message = '最新已收盘交易日尚未确认；仅展示已有历史数据，不高亮新B';
    } else if (meta.stale === true) {
      summary.status = 'stale';
      summary.message = `行情更新失败，保留${last.time}旧数据；不作为${targetDate}新B`;
    } else if (!summary.ready) {
      summary.status = 'awaiting_source';
      summary.message = `等待${targetDate}完整日K或一致的源完成标记；实际截止${last.time}`;
    } else {
      summary.status = 'ready';
      summary.message = `${targetDate}完整日K已确认`;
    }
    summary.newBuySignals = summary.ready ? summary.targetSignals.filter(isBuy) : [];
    summary.isNewBuy = summary.newBuySignals.length > 0;
    if (!signals.length) summary.message += '；近两年暂无已确认买卖信号';

    const displayedDates = new Set([summary.latestSignalDate, targetDate].filter(Boolean));
    for (const day of displayedDates) {
      const events = signals.filter(signal => signal.confirmationDate === day);
      if (events.some(isBuy) && events.some(signal => /^S[123]$/.test(signal.type))) {
        summary.message += `；${day}同日存在B与S信号，全部保留`;
      }
      const close = bars.find(bar => bar.time === day);
      if (close && events.some(signal => isBuy(signal) && close.close <= signal.stop)) {
        summary.message += `；${day} B点确认日收盘已不高于结构止损位，事件保留`;
      }
    }
    return summary;
  }

  function isFreshBuy(summary, targetDate, clockVerified = true) {
    return clockVerified === true && validDate(targetDate) && !!summary && isCn(summary.symbol)
      && summary.ready === true && summary.status === 'ready' && summary.targetDate === targetDate
      && summary.closeDate === targetDate && Array.isArray(summary.newBuySignals)
      && summary.newBuySignals.some(signal => isBuy(signal) && signal.confirmationDate === targetDate);
  }

  function sortItems(items, summaries, targetDate, clockVerified = true) {
    if (!Array.isArray(items)) return [];
    const fresh = [], rest = [];
    for (const item of items) {
      const symbol = item && item.symbol;
      const summary = summaries && Object.prototype.hasOwnProperty.call(summaries, symbol) ? summaries[symbol] : null;
      (summary && summary.symbol === symbol && isFreshBuy(summary, targetDate, clockVerified) ? fresh : rest).push(item);
    }
    return fresh.concat(rest);
  }

  return {analyzeHistory, isFreshBuy, sortItems};
});
