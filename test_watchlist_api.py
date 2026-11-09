"""Local-only watchlist and instrument-name HTTP regression tests."""
import copy
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import server
import watchlist_api as api


class WatchlistHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port
        cls.origin = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Path(self.temp.name) / 'user-data/watchlist.json'
        self.universe = Path(self.temp.name) / 'universe.json'
        self.universe.write_text(json.dumps({'snapshotDate': '2026-11-05', 'rawStocks': [
            {'symbol': 'sh600519', 'code': '600519', 'name': '贵州茅台'},
            {'symbol': 'sz300750', 'code': '300750', 'name': '宁德时代'},
            {'symbol': 'sh688001', 'code': '688001', 'name': '华兴源创'},
            {'symbol': 'sz000001', 'code': '000001', 'name': '平安银行'},
            {'symbol': 'sh600001', 'code': '600001', 'name': '*ST测试'},
            {'symbol': 'bj920000', 'code': '920000', 'name': '北交测试'}],
            'stocks': [{'symbol': '601088.SS', 'code': '601088', 'name': '中国神华'}]}, ensure_ascii=False), encoding='utf-8')
        self.patches = [patch.object(api, 'STORE_PATH', self.store), patch.object(api, 'UNIVERSE_PATH', self.universe),
                        patch.object(api, 'ETF_CACHE_PATH', Path(self.temp.name) / 'etfs.json'),
                        patch.object(server, 'urlopen', side_effect=AssertionError('no network allowed'))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, method='GET', path='/api/watchlist', body=None, headers=None):
        data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
        selected = {'Origin': self.origin, 'Content-Type': 'application/json'}
        selected.update(headers or {})
        selected = {key: value for key, value in selected.items() if value is not None}
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=7)
        try:
            conn.request(method, path, body=data, headers=selected)
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        finally:
            conn.close()

    def change(self, action='add', symbol='600519', revision=None, **kwargs):
        body = {'action': action, 'symbol': symbol}
        if revision is not None:
            body['expectedRevision'] = revision
        return self.request('POST', body=body, **kwargs)

    def test_empty_watchlist_http(self):
        status, value, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(value, {'schema': 'chanxu-watchlist-v1', 'revision': 0, 'updatedAt': None, 'items': [], 'storage': 'local_server'})
        self.assertFalse(self.store.exists())
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_add_normalize_remove_idempotent_and_persisted(self):
        status, value, _ = self.change(revision=0)
        self.assertEqual(status, 200)
        self.assertEqual(value['revision'], 1)
        self.assertEqual(value['items'][0]['symbol'], '600519.SS')
        self.assertEqual(value['items'][0]['name'], '贵州茅台')
        first = self.store.read_bytes()
        self.assertEqual(self.request()[1], value)
        self.assertEqual(self.change(symbol='600519.SS', revision=1)[1], value)
        self.assertEqual(self.store.read_bytes(), first)
        self.assertEqual(self.change('remove', 'AAPL', 1)[1], value)
        self.assertEqual(self.store.read_bytes(), first)
        program = 'import json,sys;from pathlib import Path;import server,watchlist_api as a;a.STORE_PATH=Path(sys.argv[1]);print(json.dumps(a.get_watchlist(server.normalize_symbol),ensure_ascii=False))'
        fresh = subprocess.run([sys.executable, '-X', 'utf8', '-c', program, str(self.store)], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(fresh.stdout), value)
        status, removed, _ = self.change('remove', '600519', 1)
        self.assertEqual(status, 200)
        self.assertEqual(removed['revision'], 2)
        self.assertEqual(removed['items'], [])
        self.assertEqual(json.loads(self.store.read_text(encoding='utf-8')), removed)
        self.assertEqual(list(self.store.parent.glob('*.tmp')), [])

    def test_concurrent_expected_revision_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda symbol: self.change(symbol=symbol, revision=0), ['600519', '000001']))
        self.assertEqual(sorted(row[0] for row in responses), [200, 409])
        loser = next(row[1] for row in responses if row[0] == 409)
        self.assertEqual(loser['code'], 'revision_conflict')
        self.assertEqual(self.request()[1]['revision'], 1)
        self.assertEqual(len(self.request()[1]['items']), 1)

    def test_concurrent_unconditional_add_keeps_all(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(lambda n: self.change(symbol=f'TEST{n}'), range(10)))
        self.assertTrue(all(row[0] == 200 for row in responses))
        value = self.request()[1]
        self.assertEqual(value['revision'], 10)
        self.assertEqual(len(value['items']), 10)

    def test_corrupt_file_is_never_overwritten(self):
        self.store.parent.mkdir()
        for raw in [b'{broken', b'{"schema":"bad"}', b'{"revision":0,"revision":1}']:
            with self.subTest(raw=raw):
                self.store.write_bytes(raw)
                self.assertEqual(self.request()[0:2], (409, {'ok': False, 'error': '本机自选文件损坏或无法读取，已停止写入以保留原文件', 'code': 'corrupt_watchlist'}))
                self.assertEqual(self.change()[0], 409)
                self.assertEqual(self.store.read_bytes(), raw)

    def test_corrupt_structure_duplicates_and_timestamps_rejected(self):
        good = self.change()[1]
        bad = copy.deepcopy(good); bad['items'].append(bad['items'][0])
        badtime = copy.deepcopy(good); badtime['items'][0]['addedAt'] = 'invalid'
        for doc in [bad, badtime, {**good, 'revision': True}, {**good, 'surprise': 1}]:
            raw = json.dumps(doc).encode()
            self.store.write_bytes(raw)
            self.assertEqual(self.change(symbol='AAPL')[0], 409)
            self.assertEqual(self.store.read_bytes(), raw)

    def test_atomic_replace_failure_preserves_old_file(self):
        self.change()
        old = self.store.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('mock disk error')):
            status, value, _ = self.change(symbol='AAPL')
        self.assertEqual(status, 500)
        self.assertEqual(value['code'], 'storage_unavailable')
        self.assertEqual(self.store.read_bytes(), old)
        self.assertEqual(list(self.store.parent.glob('*.tmp')), [])

    def test_capacity_200_dedup_and_remove_still_allowed(self):
        value = self.change(symbol='TEST0')[1]
        value['items'] = [{**value['items'][0], 'symbol': f'TEST{i}', 'code': f'TEST{i}', 'name': f'TEST{i}'} for i in range(200)]
        self.store.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(self.change(symbol='TEST0')[0], 200)
        status, err, _ = self.change(symbol='TEST200')
        self.assertEqual((status, err['code']), (409, 'watchlist_full'))
        self.assertEqual(self.change('remove', 'TEST0')[0], 200)
        self.assertEqual(self.change(symbol='TEST200')[0], 200)
        self.assertEqual(len(self.request()[1]['items']), 200)

    def test_client_name_extra_keys_invalid_symbols_and_types(self):
        bodies = [{'action': 'add', 'symbol': '600519', 'name': '假名称'},
                  {'action': 'add', 'symbol': '../secret'}, {'action': 'add', 'symbol': 600519},
                  {'action': 'buy', 'symbol': '600519'}, [], None,
                  {'action': 'add', 'symbol': 'AAPL', 'expectedRevision': True},
                  {'action': 'add', 'symbol': 'AAPL', 'expectedRevision': -1}]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request('POST', body=body)[0], 400)
        self.assertFalse(self.store.exists())

    def test_cross_site_null_missing_origin_and_wrong_host_blocked(self):
        cases = [{'Origin': 'https://evil.test'}, {'Origin': 'null'}, {'Origin': None},
                 {'Origin': f'http://localhost:{self.port}'}, {'Origin': self.origin + '/'},
                 {'Host': f'evil.test:{self.port}', 'Origin': f'http://evil.test:{self.port}'},
                 {'Host': '127.0.0.1:1', 'Origin': 'http://127.0.0.1:1'},
                 {'Sec-Fetch-Site': 'cross-site'}]
        for headers in cases:
            with self.subTest(headers=headers):
                status, value, result_headers = self.change(headers=headers)
                self.assertEqual((status, value['code']), (403, 'forbidden_origin'))
                self.assertNotIn('Access-Control-Allow-Origin', result_headers)
        self.assertFalse(self.store.exists())

    def test_localhost_exact_origin_allowed(self):
        status, _, _ = self.change(headers={'Host': f'localhost:{self.port}', 'Origin': f'http://localhost:{self.port}'})
        self.assertEqual(status, 200)

    def test_read_watchlist_host_guard_without_requiring_origin(self):
        self.change()
        self.assertEqual(self.request(headers={'Origin': None})[0], 200)
        for headers in [{'Host': f'evil.test:{self.port}', 'Origin': f'http://evil.test:{self.port}'},
                        {'Host': f'evil.test:{self.port}', 'Origin': None},
                        {'Origin': 'https://evil.test'}]:
            with self.subTest(headers=headers):
                status, value, _ = self.request(headers=headers)
                self.assertEqual((status, value.get('code')), (403, 'forbidden_origin'))
                self.assertNotIn('items', value)

    def test_max_revision_mutation_rejected_without_damaging_store(self):
        value = self.change()[1]
        value['revision'] = 2**53 - 1
        self.store.write_text(json.dumps(value), encoding='utf-8')
        before = self.store.read_bytes()
        self.assertEqual(self.change(symbol='600519', revision=2**53-1)[0], 200)
        status, error, _ = self.change(symbol='AAPL', revision=2**53-1)
        self.assertEqual((status, error.get('code')), (409, 'revision_exhausted'))
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.request()[0], 200)

    def test_wrong_media_oversized_json_duplicate_and_chunked_rejected(self):
        self.assertEqual(self.change(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', body=b'x' * 4097)[0], 413)
        for raw in [b'{', b'null', b'{"action":"add","action":"remove","symbol":"AAPL"}', b'{"action":"add","symbol":"AAPL","expectedRevision":NaN}']:
            self.assertEqual(self.request('POST', body=raw)[0], 400)
        self.assertEqual(self.change(headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertFalse(self.store.exists())

    def test_early_reject_receives_delayed_small_body_before_close(self):
        body = b'{"action":"add","symbol":"AAPL"}'
        for path, content_type, expected in [('/api/watchlist?file=other', 'application/json', 400),
                                             ('/api/watchlist', 'text/plain', 415),
                                             ('/api/history', 'application/json', 404)]:
            with self.subTest(path=path, content_type=content_type):
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
                try:
                    conn.putrequest('POST', path)
                    conn.putheader('Origin', self.origin)
                    conn.putheader('Content-Type', content_type)
                    conn.putheader('Content-Length', len(body))
                    conn.endheaders()
                    time.sleep(0.04)
                    conn.send(body)
                    response = conn.getresponse()
                    self.assertEqual(response.status, expected)
                    self.assertFalse(json.loads(response.read())['ok'])
                finally:
                    conn.close()
        self.assertFalse(self.store.exists())

    def test_oversized_body_is_rejected_without_waiting_for_full_length(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.putrequest('POST', '/api/watchlist')
            conn.putheader('Origin', self.origin)
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', 1024 * 1024 * 1024)
            conn.endheaders()
            started = time.monotonic()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read())['code'], 'body_too_large')
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            conn.close()

    def test_query_rejection_and_no_store_download(self):
        self.assertEqual(self.request(path='/api/watchlist?file=other')[0], 400)
        self.assertEqual(self.request('POST', path='/api/watchlist?file=other', body={'action': 'add', 'symbol': 'AAPL'})[0], 400)
        self.change()
        for path in ['/user-data/watchlist.json', '/watchlist_api.py', '/api/watchlist/../watchlist.json']:
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request('POST', path='/api/history', body={})[0], 404)

    def test_preflight_cross_origin_denied(self):
        self.assertEqual(self.request('OPTIONS', headers={'Origin': 'https://evil.test'})[0], 403)
        status, _, headers = self.request('OPTIONS')
        self.assertEqual(status, 204)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_search_code_chinese_etf_index_and_unknown(self):
        for query, symbol, name in [('600519', '600519.SS', '贵州茅台'), ('茅台', '600519.SS', '贵州茅台'),
                                    ('510300', '510300.SS', '沪深300ETF'), ('上证指数', '^SSEC', '上证指数'),
                                    ('300750', '300750.SZ', '宁德时代'), ('*ST测试', '600001.SS', '*ST测试'),
                                    ('中国神华', '601088.SS', '中国神华'), ('苹果', 'AAPL', 'AAPL')]:
            with self.subTest(query=query):
                status, result, _ = self.request(path='/api/instruments?query=' + quote(query))
                self.assertEqual(status, 200)
                self.assertIn({'symbol': symbol, 'code': api._code(symbol), 'name': name},
                              [{key: row[key] for key in ('symbol', 'code', 'name')} for row in result['items']])
                self.assertEqual(result['snapshotDate'], '2026-11-05')
        self.assertEqual(self.request(path='/api/instruments?query=doesnotexist')[1]['items'], [])
        self.assertEqual(self.request(path='/api/instruments?query=')[1]['items'], [])
        beijing = self.request(path='/api/instruments?query=920000')[1]['items']
        self.assertEqual(beijing[0]['symbol'], '920000.BJ')
        self.assertFalse(beijing[0]['historySupported'])
        self.assertTrue(beijing[0]['reason'])

    def test_search_invalid_parameters(self):
        for path in ['/api/instruments', '/api/instruments?query=1&query=2', '/api/instruments?query=a&file=x', '/api/instruments?query=' + 'x'*81]:
            self.assertEqual(self.request(path=path)[0], 400)

    def test_watchlist_does_not_apply_daily_report_universe_filters(self):
        for code, name in [('300750', '宁德时代'), ('688001', '华兴源创'), ('600001', '*ST测试'), ('510300', '沪深300ETF'), ('AAPL', 'AAPL'), ('ZZZZ', 'ZZZZ')]:
            status, value, _ = self.change(symbol=code)
            self.assertEqual(status, 200)
            self.assertEqual(value['items'][-1]['name'], name)
        self.assertEqual(len(self.request()[1]['items']), 6)

    def test_history_enrichment_is_output_only_network_fresh_stale(self):
        for cache in ('network', 'fresh', 'stale'):
            payload = {'bars': [{'time': '2026-11-04', 'close': 10}], 'meta': {'symbol': '600519.SS', 'name': 'Kweichow Moutai', 'cache': cache}}
            before = copy.deepcopy(payload)
            with patch.object(server, 'get_history', return_value=payload):
                status, value, _ = self.request(path='/api/history?symbol=600519.SS')
            self.assertEqual(status, 200)
            self.assertEqual(value['meta']['nameZh'], '贵州茅台')
            self.assertEqual(value['meta']['displayName'], '贵州茅台')
            self.assertEqual(value['meta']['name'], '贵州茅台')
            self.assertEqual(value['meta']['originalName'], 'Kweichow Moutai')
            self.assertEqual(value['bars'], before['bars'])
            self.assertEqual(payload, before)

    def test_foreign_and_missing_catalog_do_not_invent_chinese_names(self):
        payload = {'bars': [], 'meta': {'symbol': 'AAPL', 'name': 'Apple Inc.'}}
        result = api.enrich_history(payload)
        self.assertEqual(result['meta']['displayName'], 'Apple Inc.')
        self.assertIsNone(result['meta']['nameZh'])
        self.assertEqual(result['meta']['name'], 'Apple Inc.')
        self.universe.write_text('{bad', encoding='utf-8')
        self.assertEqual(api.instrument('300750.SZ')['name'], '300750.SZ')
        self.assertIn('unavailable', api.search_instruments('300750')['source'])
        self.assertEqual(api.instrument('510300.SS')['name'], '沪深300ETF')

    def test_catalog_update_seen_without_service_restart(self):
        self.assertEqual(api.instrument('300750.SZ')['name'], '宁德时代')
        doc = json.loads(self.universe.read_text(encoding='utf-8'))
        doc['rawStocks'][1]['name'] = '测试更名后'
        self.universe.write_text(json.dumps(doc), encoding='utf-8')
        self.assertEqual(api.instrument('300750.SZ')['name'], '测试更名后')


if __name__ == '__main__':
    unittest.main(verbosity=2)
