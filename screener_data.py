"""Point-in-time A-share universe and verified latest-completed-session resolver.

Universe: public Sina Finance hs_a quotation list, filtered to SH/SZ mainboard
stocks excluding ST/*ST. STAR, ChiNext, Beijing and non-stock instruments excluded.
History: existing server.get_history only; no silent price-source substitution.
Calendar: published SSE/SZSE 2026 closure notice, cross-checked against Yahoo's
completed Shanghai Composite daily bars before a dated report may be produced.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as day_time
import json
import math
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from server import DataError, get_history
from market_clock import (CN, CLOSURES, CALENDAR_SOURCE, CALENDAR_VERIFIED_ON,
                          CalendarError, market_status, now_cn,
                          is_trading_day as _is_trading_day)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "screener-data"
UNIVERSE_PATH = DATA_DIR / "universe.json"
UNIVERSE_POLICY = "sh_sz_main_no_st_v2"
SINA_API = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"


def _json_get(url):
    error = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 ChanxuResearch/0.2", "Accept": "application/json"})
            with urlopen(req, timeout=25) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise DataError(f"股票列表获取失败：{type(error).__name__}: {error}") from error


def classify_stock(raw):
    """Return (included stock or None, exclusion reason or None)."""
    code = str(raw.get("code", "")).strip()
    market = str(raw.get("symbol", ""))[:2].lower()
    name = str(raw.get("name", "")).strip()
    if not re.fullmatch(r"\d{6}", code) or market not in {"sh", "sz", "bj"} or not name:
        return None, "invalid_or_unknown_security"
    if market == "sh" and code.startswith(("688", "689")):
        return None, "科创板"
    if market == "sz" and code.startswith("30"):
        return None, "创业板"
    if market == "bj":
        return None, "北交所"
    main_sh = market == "sh" and code.startswith(("600", "601", "603", "605"))
    main_sz = market == "sz" and code.startswith(("000", "001", "002", "003"))
    if not (main_sh or main_sz):
        return None, "非目标A股或未知证券类别"
    if "ST" in name.upper():
        return None, "ST或*ST"
    return {
        "code": code,
        "name": name,
        "exchange": market.upper(),
        "symbol": code + (".SS" if main_sh else ".SZ"),
        "board": "沪市主板" if main_sh else "深市主板",
        "st": False,
        "historySupported": True,
        "coverageNote": "",
    }, None


def _page(page):
    query = urlencode({"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "init"})
    values = _json_get(SINA_API + "Market_Center.getHQNodeData?" + query)
    if not isinstance(values, list) or not all(isinstance(row, dict) for row in values):
        raise DataError(f"股票列表第{page}页不是有效数组")
    return values


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as out:
            temp_path = Path(out.name)
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_universe(force=False):
    """Complete same-day snapshot; incomplete pagination never replaces cache."""
    today = now_cn().date().isoformat()
    if not force and UNIVERSE_PATH.exists():
        try:
            cache = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
            if (cache.get("snapshotDate") == today and cache.get("complete") and cache.get("stocks")
                    and cache.get("universePolicy") == UNIVERSE_POLICY):
                return {**cache, "cache": "same-day"}
        except (OSError, ValueError):
            pass
    total = int(_json_get(SINA_API + "Market_Center.getHQNodeStockCount?node=hs_a"))
    if total < 1000 or total > 20000:
        raise DataError(f"股票列表数量异常：{total}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(_page, range(1, math.ceil(total / 100) + 1)))
    raw_rows = [row for page in pages for row in page]
    unique = {row.get("symbol") for row in raw_rows}
    if len(raw_rows) != total or len(unique) != total:
        raise DataError(f"全市场列表分页不完整或重复：count={total}, rows={len(raw_rows)}, unique={len(unique)}；拒绝生成全量扫描")
    stocks, excluded = [], []
    for raw in raw_rows:
        stock, reason = classify_stock(raw)
        if stock:
            stocks.append(stock)
        else:
            excluded.append({"code": raw.get("code"), "name": raw.get("name"), "reason": reason})
    stocks.sort(key=lambda s: (s["exchange"], s["code"]))
    payload = {
        "stocks": stocks, "excluded": excluded, "rawStocks": raw_rows,
        "source": "Sina Finance hs_a full quotation list",
        "sourceUrl": SINA_API + "Market_Center.getHQNodeData?node=hs_a",
        "fetchedAt": now_cn().isoformat(), "snapshotDate": today,
        "sourceTotal": total, "total": len(stocks), "complete": True,
        "universePolicy": UNIVERSE_POLICY,
        "excludedByReason": {reason: sum(e["reason"] == reason for e in excluded) for reason in sorted({e["reason"] for e in excluded})},
        "exclusionCountPolicy": "互斥计数：先板块（科创/创业/北交所），再沪深主板ST/*ST；一只股票只计入首个排除原因",
        "eligibleHistoryCount": sum(s["historySupported"] for s in stocks),
        "unsupportedHistoryCount": sum(not s["historySupported"] for s in stocks),
        "cache": "network", "rules": "仅沪深主板A股；排除科创板、创业板、北交所和ST（含*ST）；不含ETF、指数、B股",
    }
    _atomic_json(UNIVERSE_PATH, payload)
    return payload


def is_trading_day(value):
    """Compatibility wrapper; calendar implementation belongs to market_clock."""
    try:
        return _is_trading_day(value)
    except CalendarError as exc:
        raise DataError(str(exc)) from exc


def resolve_target_date(as_of=None):
    """Latest completed session, verified against fresh index history.

    A date-only input means 00:00 Beijing. A datetime preserves its time; a
    naive datetime is explicitly interpreted as Beijing, not host local time.
    """
    moment = as_of if as_of is not None else now_cn()
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    elif isinstance(moment, date) and not isinstance(moment, datetime):
        moment = datetime.combine(moment, day_time.min)
    if not isinstance(moment, datetime):
        raise DataError("日期参数必须是date、datetime或ISO日期时间")
    moment = moment.replace(tzinfo=CN) if moment.tzinfo is None else moment.astimezone(CN)
    try:
        status = market_status(moment)
    except CalendarError as exc:
        raise DataError(str(exc)) from exc
    target = status['targetDate']
    common = {"targetDate": target, "asOf": moment.date().isoformat(),
              "evaluatedAt": status['serverTime'], "targetPolicy": status['policy'],
              "latestCompletedSession": target, "marketStatus": status,
              "shouldRun": status['isTradingDay'], "isTradingDay": status['isTradingDay'],
              # Compatibility alias only; the new authoritative field is latestCompletedSession.
              "expectedPreviousSession": target,
              "calendarSource": CALENDAR_SOURCE, "calendarVerifiedOn": CALENDAR_VERIFIED_ON,
              "calendarYear": moment.year}
    if not status['isTradingDay']:
        return {**common, "dateVerified": False, "latestObservedTradingDate": None,
                "reason": "交易所休市，自动扫描不重复发送上一收盘日清单"}
    history = None
    error = None
    for attempt in range(3):
        try:
            history = get_history("000001.SS", "6mo", refresh=True)
            meta = history.get('meta', {})
            if meta.get('stale') or meta.get('readiness') != 'ready':
                raise DataError("上证指数行情尚未更新到目标收盘日，不能用过期或盘中数据确定信号")
            completed = [b['time'] for b in history['bars'] if b['time'] <= target]
            if not completed or max(completed) != target:
                raise DataError(f"交易时钟要求{target}，行情源最新可用日为{max(completed) if completed else '无'}；数据未齐，禁止回退旧日期推荐")
            break
        except Exception as exc:
            error = exc
            history = None
            if attempt < 2:
                time.sleep(1 + attempt)
    if history is None:
        raise DataError(str(error)) from error
    return {**common, "dateVerified": True, "latestObservedTradingDate": target,
            "indexSource": history['meta'].get('source'), "indexFetchedAt": history['meta'].get('fetchedAt')}
