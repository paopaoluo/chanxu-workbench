"""Local watchlist persistence and local-only display-name lookup.

Names are presentation metadata: never enrich scanner inputs or cached OHLCV.
The server owns the single store; its thread lock serializes read/modify/replace.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import instrument_catalog

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / 'user-data' / 'watchlist.json'
UNIVERSE_PATH = ROOT / 'screener-data' / 'universe.json'
ETF_CACHE_PATH = ROOT / 'instrument-data' / 'etfs.json'
SCHEMA = 'chanxu-watchlist-v1'
MAX_ITEMS = 200
MAX_BODY = 4096
REJECT_DRAIN_LIMIT = 64 * 1024
REJECT_DRAIN_SECONDS = 0.25
STORE_LOCK = threading.Lock()
CATALOG_LOCK = threading.Lock()
_catalog_cache = None

# Stable common labels, not a claim of a live instrument directory.
COMMON = {
    '510300.SS': ('沪深300ETF', ('沪深300', '沪深300etf')),
    '000001.SZ': ('平安银行', ()),
    '600519.SS': ('贵州茅台', ()),
    '^SSEC': ('上证指数', ('上证', '上证综指')),
    '000001.SS': ('上证指数', ()),
    '^HSI': ('恒生指数', ('恒指',)),
    'AAPL': ('AAPL', ('苹果', 'apple')),
}


class ApiError(Exception):
    def __init__(self, message, status=400, code='invalid_request'):
        super().__init__(message)
        self.status = status
        self.code = code


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON field')
        value[key] = item
    return value


def _parse_json(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError('invalid timestamp')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('timezone required')
    return stamp


def _code(symbol):
    return symbol.split('.')[0] if re.fullmatch(r'\d{6}\.(SS|SZ)', symbol) else symbol


def _valid_name(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= 160
            and not any(ord(char) < 32 for char in value))


def _catalog():
    global _catalog_cache
    signatures = []
    for path in (Path(UNIVERSE_PATH), Path(ETF_CACHE_PATH), Path(ETF_CACHE_PATH).with_name('refresh-status.json')):
        try:
            stat = path.stat()
            signatures.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signatures.append((str(path), None, None))
    # Re-evaluate stale directory age every minute without any network traffic.
    key = (tuple(signatures), int(time.time() // 60))
    with CATALOG_LOCK:
        if _catalog_cache and _catalog_cache[0] == key:
            return _catalog_cache[1]
        value = instrument_catalog.load_catalog(UNIVERSE_PATH, ETF_CACHE_PATH, COMMON)
        _catalog_cache = (key, value)
        return value


def instrument(symbol):
    entry = _catalog()['items'].get(symbol)
    # Persisted watchlist schema must not acquire search-only metadata fields.
    return {key: entry[key] for key in ('symbol', 'code', 'name')} if entry else {'symbol': symbol, 'code': _code(symbol), 'name': symbol}


def search_instruments(query):
    if not isinstance(query, str) or len(query) > 80 or any(ord(c) < 32 for c in query):
        raise ApiError('搜索内容需为不超过80字的代码或名称')
    catalog = _catalog()
    query = query.strip().casefold()
    matches = []
    if query:
        for symbol, entry in catalog['items'].items():
            values = [entry['code'], symbol, entry['name'], *catalog['aliases'].get(symbol, [])]
            folded = [value.casefold() for value in values]
            if any(query in value for value in folded):
                rank = 0 if query in folded else 1 if any(value.startswith(query) for value in folded) else 2
                matches.append((rank, symbol, entry))
    matches.sort(key=lambda row: (row[0], row[1]))
    return {'items': [dict(row[2]) for row in matches[:30]],
            'source': catalog['source'], 'snapshotDate': catalog['snapshotDate'],
            'coverage': copy.deepcopy(catalog['coverage']), 'totalMatches': len(matches), 'truncated': len(matches) > 30}


def enrich_history(payload):
    """Deep copy only at the HTTP boundary; raw caches/snapshots stay untouched."""
    result = copy.deepcopy(payload)
    meta = result.setdefault('meta', {})
    symbol = meta.get('symbol', '')
    original = meta.get('name')
    local = instrument(symbol)['name']
    chinese = local if re.search(r'[\u3400-\u9fff]', local) else None
    meta['originalName'] = original
    meta['nameZh'] = chinese
    meta['displayName'] = chinese or (original if _valid_name(original) else symbol)
    if chinese:
        meta['name'] = chinese
    return result


def _empty():
    return {'schema': SCHEMA, 'revision': 0, 'updatedAt': None, 'items': [], 'storage': 'local_server'}


def _load(normalize):
    path = Path(STORE_PATH)
    if not path.exists():
        return _empty()
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError('oversized store')
        data = _parse_json(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) != {'schema', 'revision', 'updatedAt', 'items', 'storage'}:
            raise ValueError('invalid keys')
        if data['schema'] != SCHEMA or data['storage'] != 'local_server':
            raise ValueError('invalid schema')
        revision = data['revision']
        if type(revision) is not int or not 0 <= revision < 2**53:
            raise ValueError('invalid revision')
        if not isinstance(data['items'], list) or len(data['items']) > MAX_ITEMS:
            raise ValueError('invalid items')
        if revision == 0 and (data['updatedAt'] is not None or data['items']):
            raise ValueError('invalid initial state')
        updated = _timestamp(data['updatedAt']) if revision else None
        seen = set()
        for row in data['items']:
            if not isinstance(row, dict) or set(row) != {'symbol', 'code', 'name', 'addedAt'}:
                raise ValueError('invalid item')
            symbol = row['symbol']
            if not isinstance(symbol, str) or normalize(symbol) != symbol or symbol in seen:
                raise ValueError('invalid symbol')
            if row['code'] != _code(symbol) or not _valid_name(row['name']):
                raise ValueError('invalid item metadata')
            if _timestamp(row['addedAt']) > updated:
                raise ValueError('invalid added time')
            seen.add(symbol)
        return data
    except Exception as exc:
        raise ApiError('本机自选文件损坏或无法读取，已停止写入以保留原文件', 409, 'corrupt_watchlist') from exc


def get_watchlist(normalize):
    with STORE_LOCK:
        return _load(normalize)


def update_watchlist(body, normalize):
    if (not isinstance(body, dict) or not {'action', 'symbol'} <= set(body)
            or set(body) - {'action', 'symbol', 'expectedRevision'}
            or body['action'] not in ('add', 'remove') or not isinstance(body['symbol'], str)):
        raise ApiError('仅接受 action、symbol 和可选 expectedRevision 字段')
    if 'expectedRevision' in body and (type(body['expectedRevision']) is not int or not 0 <= body['expectedRevision'] < 2**53):
        raise ApiError('expectedRevision 必须为非负整数')
    try:
        symbol = normalize(body['symbol'])
    except Exception as exc:
        raise ApiError(str(exc)) from exc
    with STORE_LOCK:
        data = _load(normalize)
        if 'expectedRevision' in body and body['expectedRevision'] != data['revision']:
            raise ApiError('自选股已在其他页面更新，请重新读取后重试', 409, 'revision_conflict')
        exists = any(item['symbol'] == symbol for item in data['items'])
        if (body['action'] == 'add' and exists) or (body['action'] == 'remove' and not exists):
            return data
        if data['revision'] >= 2**53 - 1:
            raise ApiError('自选版本号已达安全整数上限，已保留原文件并停止修改', 409, 'revision_exhausted')
        if body['action'] == 'add' and len(data['items']) >= MAX_ITEMS:
            raise ApiError('自选股最多保存200只，请先移除不需要的标的', 409, 'watchlist_full')
        stamp = datetime.now(timezone.utc).isoformat()
        if body['action'] == 'add':
            data['items'].append({**instrument(symbol), 'addedAt': stamp})
        else:
            data['items'] = [item for item in data['items'] if item['symbol'] != symbol]
        data['revision'] += 1
        data['updatedAt'] = stamp
        path = Path(STORE_PATH)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        except OSError as exc:
            raise ApiError('本机自选文件保存失败，原有文件未被覆盖', 500, 'storage_unavailable') from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return data


def validate_origin(handler, require_origin=True):
    """Block cross-site access and DNS rebinding; normal GET may omit Origin."""
    headers = handler.headers
    origins = headers.get_all('Origin', [])
    if (len(headers.get_all('Host', [])) != 1 or len(origins) > 1
            or (require_origin and len(origins) != 1)):
        raise ApiError('自选修改仅允许本机网页的同源请求', 403, 'forbidden_origin')
    host = headers['Host'].lower()
    origin = origins[0].lower() if origins else None
    port = handler.server.server_port
    if (host not in (f'127.0.0.1:{port}', f'localhost:{port}')
            or (origin is not None and origin != 'http://' + host)):
        raise ApiError('自选仅允许本机网页的同源请求', 403, 'forbidden_origin')
    if headers.get('Sec-Fetch-Site', '') not in ('', 'same-origin', 'none'):
        raise ApiError('拒绝跨站自选修改请求', 403, 'forbidden_origin')


def read_write_body(handler):
    validate_origin(handler)
    headers = handler.headers
    if headers.get('Transfer-Encoding') is not None:
        raise ApiError('不支持分块请求体')
    content_types = headers.get_all('Content-Type', [])
    if len(content_types) != 1 or content_types[0].split(';', 1)[0].strip().lower() != 'application/json':
        raise ApiError('请求必须使用 application/json', 415, 'unsupported_media_type')
    lengths = headers.get_all('Content-Length', [])
    if len(lengths) != 1 or not re.fullmatch(r'\d{1,10}', lengths[0]):
        raise ApiError('需要有效的 Content-Length')
    length = int(lengths[0])
    if length > MAX_BODY:
        raise ApiError('自选请求体过大', 413, 'body_too_large')
    if not length:
        raise ApiError('请求体不能为空')
    previous_timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(5)
        raw = handler.rfile.read(length)
        handler._watchlist_body_consumed = len(raw)
        if len(raw) != length:
            raise ValueError('incomplete body')
        return _parse_json(raw.decode('utf-8'))
    except (ValueError, UnicodeError, OSError) as exc:
        # A socket-timeout can leave BufferedReader unusable. Do not read it again.
        handler._watchlist_body_read_failed = True
        raise ApiError('请求体不是有效的 UTF-8 JSON') from exc
    finally:
        handler.connection.settimeout(previous_timeout)


def drain_rejected_body(handler):
    """Bounded discard before an early HTTP/1.0 close, avoiding Windows RST races.

    http.client/browser may send headers and body separately. Closing with its
    small body still in flight can discard the useful 400/403/415 response.
    Never wait for an arbitrarily large or slowly streamed rejected request.
    """
    if getattr(handler, '_watchlist_body_read_failed', False):
        return
    lengths = handler.headers.get_all('Content-Length', [])
    consumed = getattr(handler, '_watchlist_body_consumed', 0)
    if len(lengths) == 1 and re.fullmatch(r'\d{1,10}', lengths[0]):
        remaining = min(REJECT_DRAIN_LIMIT, max(0, int(lengths[0]) - consumed))
    elif handler.headers.get('Transfer-Encoding'):
        remaining = REJECT_DRAIN_LIMIT
    else:
        return
    if not remaining:
        return
    deadline = time.monotonic() + REJECT_DRAIN_SECONDS
    previous_timeout = handler.connection.gettimeout()
    try:
        while remaining > 0:
            wait = deadline - time.monotonic()
            if wait <= 0:
                break
            handler.connection.settimeout(wait)
            chunk = handler.rfile.read1(min(remaining, 8192))
            if not chunk:
                break
            consumed += len(chunk)
            remaining -= len(chunk)
        handler._watchlist_body_consumed = consumed
    except OSError:
        pass  # The response still rejects the request; no store access occurs.
    finally:
        handler.connection.settimeout(previous_timeout)
