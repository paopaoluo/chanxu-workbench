"""Local Yahoo Finance history gateway for the Chanxu research desk.

The browser never receives an arbitrary URL proxy.  It can only ask this
localhost service for daily OHLCV history from the fixed Yahoo chart endpoint.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import re
import tempfile
import time
from threading import Lock
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from market_clock import CN, CalendarError, market_status, is_trading_day, calendar_warnings, now_cn
import daily_report_api
import watchlist_api

_calendar_notice_day = None
_calendar_notice_lock = Lock()


def log_calendar_warnings(now=None):
    """Warn at startup and once per Beijing date while the clock is polled."""
    global _calendar_notice_day
    now = now or now_cn()
    day = now.astimezone(CN).date()
    with _calendar_notice_lock:
        if day == _calendar_notice_day:
            return
        for message in calendar_warnings(now):
            logging.getLogger('chanxu.calendar').warning('%s', message)
        _calendar_notice_day = day

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ ships zoneinfo
    ZoneInfo = None


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
RANGES = {"6mo": 180, "1y": 365, "2y": 730, "5y": 1825}
USER_AGENT = "ChanxuResearchDesk/0.1 (local research; no trading)"
COMPLETENESS_POLICY = "cn_close_v1"
COMPLETENESS_NOTE = "A股按北京时间和交易日历：15:00收盘后允许当天日K，但需行情源更新到收盘；源未更新时明确等待，不用盘中K冒充完整日K。"
FOREIGN_POLICY = "exclude_exchange_today"
FOREIGN_NOTE = "非A股仍采用保守口径，排除交易所当地日期为今天的日线。"


class DataError(Exception):
    """An expected, user-facing data request error."""


def _utc_now():
    return datetime.now(timezone.utc)


def _is_cn(symbol):
    return isinstance(symbol, str) and (symbol.endswith(('.SS','.SZ')) or symbol == '^SSEC')


def _cn_status(now=None):
    try:
        return market_status(now or _utc_now())
    except CalendarError as exc:
        raise DataError(str(exc)) from exc


def _apply_completeness(payload, now=None, stale=False):
    """Re-evaluate cutoff on every read; never rewrite original retrieval time."""
    now = now or _utc_now()
    payload = copy.deepcopy(payload)
    meta = payload['meta']
    symbol = meta['symbol']
    if not _is_cn(symbol):
        today = now.astimezone(_zone(meta.get('exchangeTimezone','UTC'), meta.get('exchangeOffsetSeconds',0))).date().isoformat()
        payload['bars'] = [b for b in payload['bars'] if b['time'] < today]
        meta.update(completenessPolicy=FOREIGN_POLICY, completenessNote=FOREIGN_NOTE)
    else:
        status = _cn_status(now)
        target = status['targetDate']
        close = datetime.fromisoformat(target+'T15:00:00+08:00')
        source_time = meta.get('sourceRegularMarketTime')
        try:
            source_epoch = float(source_time)
            source_confirmed = (close.timestamp() <= source_epoch <= now.timestamp()
                                and datetime.fromtimestamp(source_epoch, CN).date().isoformat() == target)
        except (TypeError, ValueError, OverflowError, OSError):
            source_confirmed = False
        fetched = meta.get('fetchedAt')
        try:
            fetched_after_close = datetime.fromisoformat(fetched).timestamp() >= close.timestamp()
        except (TypeError, ValueError):
            fetched_after_close = False
        target_is_past_day = target < now.astimezone(CN).date().isoformat()
        confirmed = fetched_after_close and (target_is_past_day or source_confirmed)
        payload['bars'] = [b for b in payload['bars'] if b['time'] <= target
                           and (b['time'] != target or confirmed)]
        meta.update(completenessPolicy=COMPLETENESS_POLICY, completenessNote=COMPLETENESS_NOTE,
                    expectedThrough=target, marketStatus=status,
                    sourceSessionCloseTime=close.isoformat(), sourceSessionConfirmed=confirmed,
                    sourceCloseEvidence='prior_calendar_day' if target_is_past_day and confirmed else
                                        'quote_timestamp_after_close' if confirmed else 'not_confirmed')
        last = payload['bars'][-1]['time'] if payload['bars'] else None
        ready = not stale and last == target
        meta['readiness'] = 'ready' if ready else 'awaiting_source'
        meta['readinessNote'] = (f'已取得{target}完整日K' if ready else
                                 f'等待行情源完整日K：目标{target}，实际{last or "无"}' if not stale else
                                 f'在线更新失败；仅展示旧数据，目标{target}，实际{last or "无"}')
    if len(payload['bars']) < 5:
        raise DataError('行情源返回的可用历史日线不足 5 根，请扩大范围或使用 CSV 备用')
    meta['asOf'] = payload['bars'][-1]['time']
    meta['completeThrough'] = meta['asOf']
    return payload


def normalize_symbol(raw: str) -> str:
    value = (raw or "").strip().upper()
    if not value:
        raise DataError("请输入行情代码，例如 510300.SS、000001.SZ 或 AAPL")
    if re.fullmatch(r"\d{6}", value):
        if value.startswith(("4", "8", "92")):
            raise DataError("该北交所/股转代码不受当前行情源支持，不能作为上海代码读取；请在中文搜索候选中查看市场及支持状态")
        if value[0] in "569":
            return value + ".SS"
        if value[0] in "0123":
            return value + ".SZ"
        raise DataError("暂不能自动识别该六位代码的交易所，请使用带 .SS 或 .SZ 的代码")
    # Index symbols and conventional US/HK ticker forms.  No slash, colon,
    # query, or hostname characters are accepted, so this cannot be a proxy.
    if not (re.fullmatch(r"\d{6}\.(SS|SZ)", value) or re.fullmatch(r"\^?[A-Z][A-Z0-9.=^_-]{0,19}", value)):
        raise DataError("行情代码格式不支持，请使用 510300.SS、000001.SZ、^SSEC 或 AAPL；此接口不支持中文名称搜索")
    return value


def _cache_path(symbol: str, period: str) -> Path:
    digest = hashlib.sha256(f"{symbol}:{period}:1d".encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


def _zone(exchange_tz: str, offset: int = 0):
    try:
        return ZoneInfo(exchange_tz) if ZoneInfo and exchange_tz else timezone(timedelta(seconds=offset))
    except Exception:
        return timezone(timedelta(seconds=offset))


def _exchange_date(timestamp: int, exchange_tz: str, offset: int = 0):
    return datetime.fromtimestamp(timestamp, _zone(exchange_tz, offset)).date()


def _fetch(symbol: str, period: str) -> dict:
    params = {"range": period, "interval": "1d", "events": "history", "includeAdjustedClose": "true"}
    request = Request(YAHOO_CHART.format(symbol) + "?" + urlencode(params), headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise DataError(f"Yahoo 行情源请求失败（HTTP {exc.code}），请稍后重试或使用 CSV 备用") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DataError("Yahoo 行情源连接失败或数据无效，请检查网络后重试，或使用 CSV 备用") from exc
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    error = (payload.get("chart") or {}).get("error")
    if not result:
        message = (error or {}).get("description") if isinstance(error, dict) else None
        raise DataError("Yahoo 未返回该标的的日线，请核对代码和历史范围，或使用 CSV 备用")
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [None])[0] or {}
    exchange_tz = meta.get("exchangeTimezoneName") or "UTC"
    offset = int(meta.get("gmtoffset") or 0)
    now = _utc_now()
    cn_status = _cn_status(now) if _is_cn(symbol) else None
    today_exchange = now.astimezone(_zone(exchange_tz, offset)).date()
    bars = []
    close_fallback = []
    for index, timestamp in enumerate(timestamps):
        if timestamp is None:
            continue
        try:
            ts = int(timestamp)
            date = datetime.fromtimestamp(ts, CN).date() if cn_status else _exchange_date(ts, exchange_tz, offset)
            values = {field: (quote.get(field) or [None] * len(timestamps))[index] for field in ("open", "high", "low", "close", "volume")}
            # Yahoo sometimes leaves the latest daily close null even after the
            # exchange closed. Its SAME-response, same-session regular quote
            # can fill only that field, with explicit provenance and bounds.
            fallback = None
            if cn_status and date.isoformat() == cn_status['targetDate'] and values['close'] is None:
                try:
                    quote_time = int(meta['regularMarketTime'])
                    quote_price = float(meta['regularMarketPrice'])
                    quote_date = datetime.fromtimestamp(quote_time, CN).date()
                    close_time = datetime.fromisoformat(date.isoformat()+'T15:00:00+08:00').timestamp()
                    low, high = float(values['low']), float(values['high'])
                    # Daily extremes may be Float32 while the same-response
                    # regular quote is decimal-rounded (e.g. index .173 vs
                    # .172607421875). Never change those extremes: clamp only
                    # a tiny representational discrepancy and retain evidence.
                    tolerance = max(0.0005, abs(quote_price) * 2**-23)
                    if (quote_date == date and close_time <= quote_time <= now.timestamp()
                            and math.isfinite(quote_price) and quote_price > 0
                            and math.isfinite(low) and math.isfinite(high) and low <= high
                            and low-tolerance <= quote_price <= high+tolerance):
                        applied_price = min(high, max(low, quote_price))
                        values['close'] = applied_price
                        fallback = {'date':date.isoformat(), 'source':'meta.regularMarketPrice',
                                    'value':applied_price, 'reportedQuoteValue':quote_price,
                                    'roundingAdjustment':applied_price-quote_price,
                                    'tolerance':tolerance, 'sourceRegularMarketTime':quote_time,
                                    'reason':'daily close null; same-session quote at or after 15:00 Beijing'}
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
            if any(value is None for value in values.values()):
                continue
            numbers = {key: float(value) for key, value in values.items()}
            if not all(math.isfinite(value) for value in numbers.values()):
                continue
            if not cn_status and date >= today_exchange:
                continue
            if min(numbers["open"], numbers["high"], numbers["low"], numbers["close"]) <= 0 or numbers["volume"] < 0:
                continue
            if numbers["high"] < max(numbers["open"], numbers["close"], numbers["low"]):
                continue
            if numbers["low"] > min(numbers["open"], numbers["close"], numbers["high"]):
                continue
            bars.append({"time": date.isoformat(), "open": numbers["open"], "high": numbers["high"], "low": numbers["low"], "close": numbers["close"], "volume": numbers["volume"]})
            if fallback:
                close_fallback.append(fallback)
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    if len(bars) < 5:
        raise DataError("行情源返回的可用历史日线不足 5 根，请扩大范围或使用 CSV 备用")
    for prior, current in zip(bars, bars[1:]):
        if current["time"] <= prior["time"]:
            raise DataError("行情源返回了重复或倒序日期，已拒绝分析，请重试或使用有效 CSV")
    name = meta.get("longName") or meta.get("shortName") or symbol
    result = {"bars": bars, "meta": {"symbol": symbol, "name": name, "currency": meta.get("currency") or "", "source": "Yahoo Finance chart API", "adjustment": "unadjusted OHLC; adjusted close is not applied", "asOf": bars[-1]["time"], "range": period, "interval": "1d", "exchangeTimezone": 'Asia/Shanghai' if cn_status else exchange_tz, "exchangeOffsetSeconds":28800 if cn_status else offset, "completeThrough": bars[-1]["time"], "stale": False, "fetchedAt": now.astimezone(timezone.utc).isoformat(), "cacheAgeSeconds": 0,
        "sourceRegularMarketTime":meta.get('regularMarketTime'), "closeFieldFallback":close_fallback}}
    return _apply_completeness(result, now)


def _cache_metadata(payload: dict, cache: str, cached_at: float, now: float) -> dict:
    """Cache freshness describes retrieval age, never exchange-calendar completeness."""
    original_policy = payload.get('meta',{}).get('completenessPolicy')
    payload = _apply_completeness(payload, _utc_now(), stale=cache=='stale')
    payload["meta"] = {**payload.get("meta", {}), "cache": cache, "stale": cache == "stale",
                       "cacheAgeSeconds": max(0, int(now - cached_at))}
    if cache != 'network':
        payload['meta']['cachedCompletenessPolicy'] = original_policy
    if not payload["meta"].get("fetchedAt"):
        payload["meta"]["fetchedAt"] = datetime.fromtimestamp(cached_at, timezone.utc).isoformat()
    if cache == "stale":
        payload["meta"]["staleReason"] = "在线更新失败，保留上次数据；缓存时间不代表行情截止日"
    return payload


def _fresh_cache_allowed(payload, now):
    meta = payload.get('meta', {})
    if not _is_cn(meta.get('symbol','')):
        return meta.get('completenessPolicy') == FOREIGN_POLICY
    status = _cn_status(now)
    fetched_status = meta.get('marketStatus') or {}
    # Midnight is a new check even when the completed session stays the same.
    if not (meta.get('completenessPolicy') == COMPLETENESS_POLICY
            and meta.get('expectedThrough') == status['targetDate']
            and meta.get('asOf') == status['targetDate']
            and meta.get('readiness') == 'ready' and not meta.get('stale')
            and str(fetched_status.get('serverTime',''))[:10] == status['serverTime'][:10]):
        return False
    return True


def get_history(raw_symbol: str, period: str, refresh: bool = False) -> dict:
    symbol = normalize_symbol(raw_symbol)
    if period not in RANGES:
        raise DataError("历史范围仅支持近 6 个月、1 年、2 年或 5 年")
    path = _cache_path(symbol, period)
    now = time.time()
    if not refresh and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (0 <= now - float(cached.get("cachedAt", 0)) < 900 and cached.get("payload", {}).get("bars")
                    and _fresh_cache_allowed(cached['payload'], _utc_now())):
                payload = cached["payload"]
                return _cache_metadata(payload, "fresh", float(cached["cachedAt"]), now)
        except (OSError, ValueError, TypeError):
            pass
    try:
        payload = _fetch(symbol, period)
        # Publish complete JSON atomically when concurrent read requests overlap.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump({"cachedAt": now, "payload": payload}, output, ensure_ascii=False, allow_nan=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return _cache_metadata(payload, "network", now, now)
    except DataError:
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                payload = cached["payload"]
                return _cache_metadata(payload, "stale", float(cached.get("cachedAt", 0)), now)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "ChanxuLocal/0.1"

    def _send_json(self, status: int, body: dict):
        if self.command == 'POST' and status >= 400:
            watchlist_api.drain_rejected_body(self)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if urlparse(self.path).path != '/api/watchlist':
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A cancelled browser request is not a data-service failure.

    def do_OPTIONS(self):
        if urlparse(self.path).path == '/api/watchlist':
            try:
                watchlist_api.validate_origin(self)
                self.send_response(204)
                self.send_header('Allow', 'GET, POST, OPTIONS')
                self.end_headers()
            except watchlist_api.ApiError as exc:
                self._send_json(exc.status, {'ok': False, 'error': str(exc), 'code': exc.code})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/watchlist':
            self._send_json(404, {'ok': False, 'error': 'not found'})
            return
        try:
            if parsed.query:
                raise watchlist_api.ApiError('自选修改不接受 URL 查询参数')
            body = watchlist_api.read_write_body(self)
            self._send_json(200, watchlist_api.update_watchlist(body, normalize_symbol))
        except watchlist_api.ApiError as exc:
            self._send_json(exc.status, {'ok': False, 'error': str(exc), 'code': exc.code})
        except Exception:
            self._send_json(500, {'ok': False, 'error': '本机自选服务暂不可用', 'code': 'storage_unavailable'})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ('/api/watchlist', '/api/instruments'):
            try:
                if parsed.path == '/api/watchlist':
                    watchlist_api.validate_origin(self, require_origin=False)
                    if parsed.query:
                        raise watchlist_api.ApiError('自选读取不接受 URL 查询参数')
                    value = watchlist_api.get_watchlist(normalize_symbol)
                else:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {'query'} or len(query['query']) != 1:
                        raise watchlist_api.ApiError('需要唯一的 query 搜索参数')
                    value = watchlist_api.search_instruments(query['query'][0])
                self._send_json(200, value)
            except watchlist_api.ApiError as exc:
                self._send_json(exc.status, {'ok': False, 'error': str(exc), 'code': exc.code})
            except Exception:
                self._send_json(500, {'ok': False, 'error': '本机标的服务暂不可用', 'code': 'local_service_unavailable'})
            return
        if parsed.path == '/api/daily-signals':
            self._send_json(200, daily_report_api.get_daily_signals())
            return
        if parsed.path == '/api/daily-signals/snapshot':
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) != {'report','sha','code'} or any(len(v)!=1 for v in query.values()):
                    raise daily_report_api.ApiError('需要唯一的report、sha和code参数',400,'invalid_parameters')
                value=daily_report_api.get_snapshot(query['report'][0],query['sha'][0],query['code'][0])
                self._send_json(200,value)
            except daily_report_api.ApiError as exc:
                self._send_json(exc.status,{'ok':False,'error':str(exc),'code':exc.code})
            except Exception:
                self._send_json(409,{'ok':False,'error':'快照文件无效或暂时不可读取','code':'invalid_snapshot'})
            return
        if parsed.path == '/api/market-status':
            log_calendar_warnings()
            try:
                self._send_json(200, _cn_status())
            except DataError as exc:
                self._send_json(503, {'ok':False, 'error':str(exc)})
            return
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "chanxu-local-data", "source": "Yahoo Finance chart API"})
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            try:
                symbol = query.get("symbol", [""])[0]
                period = query.get("range", ["2y"])[0]
                refresh = query.get("refresh", ["0"])[0] == "1"
                self._send_json(200, watchlist_api.enrich_history(get_history(symbol, period, refresh)))
            except DataError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # do not leak a traceback to the client
                self._send_json(502, {"ok": False, "error": "本机数据服务暂不可用，请稍后重试或使用 CSV 备用"})
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            target = BASE_DIR / "index.html"
        elif parsed.path in {"/styles.css", "/app.js", "/engine.js", "/watchlist_monitor.js"}:
            target = BASE_DIR / parsed.path.lstrip("/")
        else:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            target = target.resolve()
            target.relative_to(BASE_DIR)
            data = target.read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"ok": False, "error": "file not found"})
            return
        content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "application/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Chanxu localhost data gateway")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    log_calendar_warnings()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Chanxu running at http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
