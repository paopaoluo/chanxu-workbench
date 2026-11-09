"""Local instrument directory; network refresh is explicit, never part of search."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'instrument-data'
ETF_CACHE_PATH = DATA_DIR / 'etfs.json'
SCHEMA = 'chanxu-etf-catalog-v1'
CN = timezone(timedelta(hours=8))
SINA_API = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
ETF_NODE = 'etf_hq_fund'
ETF_SOURCE = 'Sina Finance etf_hq_fund directory'
ETF_URL = SINA_API + 'Market_Center.getHQNodeData?node=' + ETF_NODE


def _now():
    return datetime.now(CN)


def _json(path, maximum=20 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValueError('目录文件过大')
    return json.loads(path.read_text(encoding='utf-8'))


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def _name(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 160 and not any(ord(c) < 32 for c in value)


def normalize_entry(row, kind='stock'):
    if not isinstance(row, dict) or not _name(row.get('name')):
        raise ValueError('目录名称无效')
    code, symbol = row.get('code'), row.get('symbol')
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or not isinstance(symbol, str):
        raise ValueError('目录代码无效')
    raw = re.fullmatch(r'(sh|sz|bj)(\d{6})', symbol, re.I)
    if raw:
        if raw.group(2) != code:
            raise ValueError('目录代码与交易所标识不一致')
        exchange = raw.group(1).upper()
        symbol = code + {'SH': '.SS', 'SZ': '.SZ', 'BJ': '.BJ'}[exchange]
    else:
        suffix = re.fullmatch(r'\d{6}\.(SS|SZ|BJ)', symbol)
        if not suffix or symbol[:6] != code:
            raise ValueError('目录交易所无效')
        exchange = {'SS': 'SH', 'SZ': 'SZ', 'BJ': 'BJ'}[suffix.group(1)]
    if kind == 'etf' and exchange == 'BJ':
        raise ValueError('ETF源包含不支持的交易所')
    entry = {'symbol': symbol, 'code': code, 'name': row['name'].strip(), 'kind': kind,
             'exchange': exchange, 'historySupported': exchange != 'BJ',
             'reason': '北交所标的可检索；当前行情源不支持，不能加载K线' if exchange == 'BJ' else ''}
    return entry


def search_aliases(entry, row=None):
    values = [entry['name']]
    if entry['kind'] == 'stock':
        clean = re.sub(r'^(?:(?:XD|XR|DR|S\*ST|SST|\*ST|ST)|[NC])+', '', entry['name'], flags=re.I)
        if clean and clean != entry['name'] and re.search(r'[\u3400-\u9fff]', clean):
            values.append(clean)
    if isinstance(row, dict):
        for field in ('fullName', 'fullname', 'shortName'):
            if _name(row.get(field)):
                values.append(row[field].strip())
    return values


def _stock_catalog(path):
    entries, aliases = {}, {}
    info = {'count': 0, 'sourceCount': None, 'complete': False, 'status': 'missing',
            'snapshotDate': None, 'source': 'local A-share universe', 'scope': '沪深北A股证券名称；不应用每日筛选的板块/ST排除'}
    try:
        data = _json(path)
        if not isinstance(data, dict):
            raise ValueError('股票目录不是对象')
        raw_full = isinstance(data.get('rawStocks'), list)
        rows = data.get('rawStocks') if raw_full else data.get('stocks')
        if not isinstance(rows, list):
            raise ValueError('股票目录缺少证券数组')
        invalid = 0
        for row in rows:
            try:
                entry = normalize_entry(row)
                if entry['symbol'] in entries:
                    invalid += 1
                    continue
                entries[entry['symbol']] = entry
                aliases[entry['symbol']] = search_aliases(entry, row)
            except ValueError:
                invalid += 1
        # Older local files may have an incomplete raw directory plus a stock
        # subset. Keep extra known names, but the count check below stays partial.
        for row in data.get('stocks', []) if raw_full and isinstance(data.get('stocks'), list) else []:
            try:
                entry = normalize_entry(row)
                if entry['symbol'] not in entries:
                    entries[entry['symbol']] = entry
                    aliases[entry['symbol']] = search_aliases(entry, row)
            except ValueError:
                pass
        source_count = data.get('sourceTotal')
        source_count = source_count if type(source_count) is int and source_count >= 0 else None
        day = _date(data.get('snapshotDate'))
        complete = raw_full and data.get('complete') is True and bool(day) and not invalid and len(entries) == len(rows) == source_count
        info.update(count=len(entries), sourceCount=source_count, complete=complete,
                    status='ready' if complete else 'partial', snapshotDate=day,
                    source=data.get('source') if isinstance(data.get('source'), str) else info['source'])
        if not complete:
            info['message'] = '股票目录仅按实际有效记录检索，不能声称覆盖源全量'
    except FileNotFoundError:
        info['message'] = '股票目录尚未缓存'
    except (OSError, ValueError, TypeError):
        info.update(status='invalid', message='股票目录损坏或格式无效，未将筛选池冒充全市场')
        entries, aliases = {}, {}
    return entries, aliases, info


def validate_etf_cache(data):
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('items'), list):
        raise ValueError('ETF缓存格式无效')
    if not _date(data.get('snapshotDate')) or not isinstance(data.get('fetchedAt'), str):
        raise ValueError('ETF缓存日期无效')
    stamp = datetime.fromisoformat(data['fetchedAt'])
    if stamp.tzinfo is None:
        raise ValueError('ETF缓存缺少时区')
    entries = {}
    for row in data['items']:
        entry = normalize_entry(row, 'etf')
        if entry['symbol'] in entries:
            raise ValueError('ETF缓存重复')
        entries[entry['symbol']] = entry
    if (data.get('complete') is not True or type(data.get('sourceTotal')) is not int
            or len(entries) != data.get('sourceTotal') or len(entries) != data.get('total') or not entries):
        raise ValueError('ETF缓存数量与来源总数不一致')
    return entries


def _etf_catalog(path, now=None):
    now = now or _now()
    info = {'count': 0, 'sourceCount': None, 'complete': False, 'status': 'missing',
            'snapshotDate': None, 'source': ETF_SOURCE, 'scope': 'Sina etf_hq_fund节点沪深ETF；非所有全球证券'}
    entries = {}
    try:
        data = _json(path)
        entries = validate_etf_cache(data)
        age = (now - datetime.fromisoformat(data['fetchedAt'])).total_seconds()
        info.update(count=len(entries), sourceCount=data['sourceTotal'], complete=True,
                    status='stale' if age > 86400 or age < -300 else 'ready',
                    snapshotDate=data['snapshotDate'], fetchedAt=data['fetchedAt'], source=data.get('source', ETF_SOURCE))
    except FileNotFoundError:
        info['message'] = 'ETF目录尚未下载；请运行独立目录刷新脚本'
    except (OSError, ValueError, TypeError):
        info.update(status='invalid', message='ETF缓存损坏或不完整；不宣称全量ETF覆盖')
        entries = {}
    try:
        attempt = _json(Path(path).with_name('refresh-status.json'), 256 * 1024)
        if isinstance(attempt, dict) and attempt.get('ok') is False:
            info.update(lastAttemptAt=attempt.get('attemptedAt'), lastError=attempt.get('error'))
            if entries:
                info['status'] = 'stale'
                info['message'] = '最近一次目录刷新失败；保留上次有效目录及原日期'
    except (OSError, ValueError, TypeError):
        pass
    return entries, {symbol: search_aliases(entry) for symbol, entry in entries.items()}, info


def load_catalog(universe_path, etf_path, common, now=None):
    stocks, stock_aliases, stock_info = _stock_catalog(universe_path)
    etfs, etf_aliases, etf_info = _etf_catalog(etf_path, now)
    entries, aliases = {}, {}
    for symbol, (name, other) in common.items():
        exchange = 'SH' if symbol.endswith('.SS') or symbol == '^SSEC' else 'SZ' if symbol.endswith('.SZ') else 'HK' if symbol == '^HSI' else 'US'
        kind = 'etf' if symbol == '510300.SS' else 'index' if symbol in ('^SSEC', '000001.SS', '^HSI') else 'stock' if symbol.endswith(('.SS', '.SZ')) else 'foreign'
        code = symbol.split('.')[0] if re.fullmatch(r'\d{6}\.(SS|SZ)', symbol) else symbol
        entries[symbol] = {'symbol': symbol, 'code': code, 'name': name, 'kind': kind, 'exchange': exchange, 'historySupported': True, 'reason': ''}
        aliases[symbol] = [name, *other]
    for group, group_aliases in [(stocks, stock_aliases), (etfs, etf_aliases)]:
        for symbol, entry in group.items():
            entries[symbol] = entry
            aliases.setdefault(symbol, []).extend(group_aliases[symbol])
    common_only = sum(symbol not in stocks and symbol not in etfs for symbol in common)
    dates = [info['snapshotDate'] for info in (stock_info, etf_info) if info['snapshotDate']]
    coverage = {'total': len(entries), 'historySupported': sum(e['historySupported'] for e in entries.values()),
                'historyUnsupported': sum(not e['historySupported'] for e in entries.values()),
                'complete': stock_info['complete'] and etf_info['complete'], 'stocks': stock_info, 'etfs': etf_info,
                'common': {'count': common_only}, 'scope': '沪深北A股目录 + 沪深ETF目录缓存 + 少量常用指数/境外别名',
                'note': 'complete仅表示缓存记录与采集时来源节点总数一致，不保证交易所永久全量；目录日期不是行情日期；名称为来源证券简称，不声称完整公司法定全称；historySupported仅表示行情路由支持，不保证每只都有可用历史数据'}
    unavailable = stock_info['status'] in ('missing', 'invalid') or etf_info['status'] in ('missing', 'invalid')
    return {'items': entries, 'aliases': aliases, 'coverage': coverage,
            'source': 'local_universe+Sina_ETF_cache+common_aliases' + ('; local_catalog_unavailable' if unavailable else ''),
            'snapshotDate': min(dates) if dates else None}


def _atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as out:
            temporary = Path(out.name)
            json.dump(value, out, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            out.flush(); os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_etfs(cache_path=ETF_CACHE_PATH, force=False, timeout=15, now=None, opener=None):
    """Explicit bounded refresh; only a complete verified set replaces the cache."""
    cache_path = Path(cache_path); now = now or _now(); opener = opener or urlopen
    status_path = cache_path.with_name('refresh-status.json')
    old = None
    try:
        old = _json(cache_path); validate_etf_cache(old)
        age = (now - datetime.fromisoformat(old['fetchedAt'])).total_seconds()
        if not force and 0 <= age < 86400:
            return {'ok': True, 'cache': 'fresh', 'total': old['total'], 'snapshotDate': old['snapshotDate'], 'source': old['source']}
    except (OSError, ValueError, TypeError):
        old = None
    run = now.strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8]
    raw_dir = cache_path.parent / 'raw' / run; raw_dir.mkdir(parents=True, exist_ok=False)
    evidence = []
    def fetch(label, method, params):
        url = SINA_API + method + '?' + urlencode(params)
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 ChanxuLocalCatalog/0.1', 'Referer': 'https://finance.sina.com.cn/'})
        with opener(request, timeout=timeout) as response:
            raw = response.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError('目录响应过大')
            record = {'label': label, 'url': url, 'fetchedAt': _now().isoformat(), 'status': getattr(response, 'status', 200),
                      'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest().upper(), 'rawFile': label + '.raw'}
        (raw_dir / record['rawFile']).write_bytes(raw)
        evidence.append(record)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('gb18030')
        return json.loads(text)
    try:
        count = int(fetch('count-before', 'Market_Center.getHQNodeStockCount', {'node': ETF_NODE}))
        if not 100 <= count <= 20000:
            raise ValueError('ETF来源总数异常，拒绝发布')
        def page(number):
            rows = fetch(f'page-{number:03}', 'Market_Center.getHQNodeData', {'node': ETF_NODE, 'page': number, 'num': 100, 'sort': 'symbol', 'asc': 1})
            if not isinstance(rows, list) or len(rows) != min(100, count - (number - 1) * 100):
                raise ValueError(f'ETF第{number}页条数不完整')
            return rows
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(page, range(1, math.ceil(count / 100) + 1)))
        rows = [row for group in pages for row in group]
        entries = [normalize_entry(row, 'etf') for row in rows]
        if len(entries) != count or len({item['symbol'] for item in entries}) != count:
            raise ValueError('ETF分页重复或数量不匹配，拒绝发布')
        after = int(fetch('count-after', 'Market_Center.getHQNodeStockCount', {'node': ETF_NODE}))
        if after != count:
            raise ValueError('ETF来源数量在采集期间变化，拒绝发布')
        doc = {'schema': SCHEMA, 'source': ETF_SOURCE, 'sourceUrl': ETF_URL, 'fetchedAt': now.isoformat(),
               'snapshotDate': now.astimezone(CN).date().isoformat(), 'sourceTotal': count, 'total': len(entries),
               'complete': True, 'items': entries, 'rawRun': run}
        validate_etf_cache(doc)
        _atomic_json(raw_dir / 'manifest.json', {'ok': True, 'sourceTotal': count, 'responses': sorted(evidence, key=lambda row: row['label'])})
        _atomic_json(cache_path, doc)
        result = {'ok': True, 'cache': 'network', 'total': count, 'snapshotDate': doc['snapshotDate'], 'source': ETF_SOURCE,
                  'attemptedAt': now.isoformat(), 'rawRun': run}
        try:
            _atomic_json(status_path, result)
        except OSError:
            # The verified directory is already committed. Do not falsely say
            # the old cache was retained merely because status logging failed.
            result['warning'] = '有效目录已保存，但刷新状态记录写入失败'
        return result
    except Exception as exc:
        message = f'{type(exc).__name__}: {str(exc)[:220]}'
        result = {'ok': False, 'cache': 'stale' if old else 'missing', 'error': message, 'attemptedAt': now.isoformat(),
                  'retainedSnapshotDate': old['snapshotDate'] if old else None, 'rawRun': run}
        _atomic_json(raw_dir / 'manifest.json', {**result, 'responses': sorted(evidence, key=lambda row: row['label'])})
        _atomic_json(status_path, result)
        return result
