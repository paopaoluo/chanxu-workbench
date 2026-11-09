"""Offline catalog/search contracts; live capture is a separate evidence run."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse
import instrument_catalog as catalog
import watchlist_api as api
import server


NOW = datetime(2026, 11, 5, 11, 0, tzinfo=catalog.CN)


class Response:
    status = 200
    def __init__(self, value): self.raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size=-1): return self.raw if size < 0 else self.raw[:size]


def etf_rows(count=102):
    return [{'symbol': f'sh{510000+i:06}', 'code': f'{510000+i:06}', 'name': f'测试ETF{i}'} for i in range(count)]


def cache_doc(rows, date=NOW):
    return {'schema': catalog.SCHEMA, 'source': catalog.ETF_SOURCE, 'sourceUrl': catalog.ETF_URL,
            'fetchedAt': date.isoformat(), 'snapshotDate': date.date().isoformat(),
            'sourceTotal': len(rows), 'total': len(rows), 'complete': True,
            'items': [catalog.normalize_entry(row, 'etf') for row in rows]}


class CatalogContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stock = self.root / 'universe.json'
        self.etfs = self.root / 'etfs.json'
        self.store = self.root / 'watchlist.json'
        rows = [
            {'symbol': 'sz002156', 'code': '002156', 'name': '通富微电'},
            {'symbol': 'sh600186', 'code': '600186', 'name': 'XD莲花控'},
            {'symbol': 'sh600036', 'code': '600036', 'name': '招商银行', 'fullName': '招商银行股份有限公司'},
            {'symbol': 'sz300750', 'code': '300750', 'name': '宁德时代'},
            {'symbol': 'sh688001', 'code': '688001', 'name': '华兴源创'},
            {'symbol': 'sh600001', 'code': '600001', 'name': '*ST测试'},
            {'symbol': 'bj920000', 'code': '920000', 'name': '安徽凤凰'}]
        self.stock_doc = {'rawStocks': rows, 'stocks': rows[:3], 'sourceTotal': len(rows), 'complete': True,
                          'source': 'fixture A-share directory', 'snapshotDate': '2026-11-05'}
        self.stock.write_text(json.dumps(self.stock_doc, ensure_ascii=False), encoding='utf-8')
        self.etf_doc = cache_doc([
            {'symbol': 'sh510880', 'code': '510880', 'name': '红利ETF'},
            {'symbol': 'sz159951', 'code': '159951', 'name': '红利ETF'},
            {'symbol': 'sh512880', 'code': '512880', 'name': '证券ETF'},
            {'symbol': 'sh512480', 'code': '512480', 'name': '半导体ETF'}])
        self.etfs.write_text(json.dumps(self.etf_doc, ensure_ascii=False), encoding='utf-8')
        self.patches = [patch.object(api, 'UNIVERSE_PATH', self.stock), patch.object(api, 'ETF_CACHE_PATH', self.etfs),
                        patch.object(api, 'STORE_PATH', self.store), patch.object(catalog, '_now', return_value=NOW),
                        patch.object(catalog, 'urlopen', side_effect=AssertionError('search must never access network'))]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_search_reports_coverage_and_match_count(self):
        value = api.search_instruments('通富')
        self.assertIn('coverage', value)
        self.assertIn('totalMatches', value)
        self.assertIn('truncated', value)
        self.assertEqual(value['coverage']['stocks']['count'], 7)
        self.assertEqual(value['coverage']['etfs']['count'], 4)
        self.assertTrue(value['coverage']['complete'])

    def test_non_common_stock_full_name_fragment_and_original_status_name(self):
        for query, symbol in [('莲花', '600186.SS'), ('莲花控', '600186.SS'), ('通富', '002156.SZ'),
                              ('招商', '600036.SS'), ('招商银行股份有限公司', '600036.SS')]:
            self.assertIn(symbol, [row['symbol'] for row in api.search_instruments(query)['items']])
        self.assertEqual(api.search_instruments('莲花')[ 'items'][0]['name'], 'XD莲花控')

    def test_universe_is_raw_all_boards_not_eligible_daily_pool(self):
        for query in ['300750', '688001', '*ST测试']:
            self.assertTrue(api.search_instruments(query)['items'])
        row = api.search_instruments('安徽凤凰')['items'][0]
        self.assertEqual(row['symbol'], '920000.BJ')
        self.assertEqual(row['exchange'], 'BJ')
        self.assertFalse(row['historySupported'])
        self.assertTrue(row['reason'])

    def test_etf_same_name_never_collapses_to_one_common_alias(self):
        result = api.search_instruments('红利')
        self.assertEqual(result['totalMatches'], 2)
        self.assertEqual({row['symbol'] for row in result['items']}, {'510880.SS', '159951.SZ'})
        for query in ['证券', '半导体']:
            row = api.search_instruments(query)['items'][0]
            self.assertEqual(row['kind'], 'etf')
            self.assertTrue(row['historySupported'])

    def test_exact_then_prefix_then_contains_and_truncation_count(self):
        rows = [{'symbol': f'sh{510500+i}', 'code': str(510500+i), 'name': f'红利ETF测试{i}'} for i in range(32)]
        rows += [{'symbol': 'sh510900', 'code': '510900', 'name': '红利'},
                 {'symbol': 'sh510001', 'code': '510001', 'name': '银行红利ETF'}]
        self.etfs.write_text(json.dumps(cache_doc(rows)), encoding='utf-8')
        result = api.search_instruments('红利')
        self.assertEqual(result['items'][0]['symbol'], '510900.SS')
        self.assertNotIn('510001.SS', [row['symbol'] for row in result['items']])
        self.assertEqual(len(result['items']), 30)
        self.assertEqual(result['totalMatches'], 34)
        self.assertTrue(result['truncated'])

    def test_missing_and_invalid_etf_cache_are_explicit_no_false_full_coverage(self):
        self.etfs.unlink()
        result = api.search_instruments('红利')
        self.assertEqual(result['coverage']['etfs']['status'], 'missing')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['items'], [])
        self.etfs.write_text('{broken', encoding='utf-8')
        self.assertEqual(api.search_instruments('红利')['coverage']['etfs']['status'], 'invalid')

    def test_incomplete_stock_cache_does_not_claim_complete(self):
        self.stock_doc['sourceTotal'] += 1
        self.stock.write_text(json.dumps(self.stock_doc), encoding='utf-8')
        result = api.search_instruments('莲花')
        self.assertEqual(result['coverage']['stocks']['status'], 'partial')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['totalMatches'], 1)

    def test_common_alias_preserves_actual_etf_name_and_alternatives(self):
        rows = [{'symbol':'sh510300','code':'510300','name':'沪深300ETF华泰柏瑞'},
                {'symbol':'sz159919','code':'159919','name':'沪深300ETF嘉实'}]
        self.etfs.write_text(json.dumps(cache_doc(rows)),encoding='utf-8')
        result=api.search_instruments('沪深300')
        self.assertEqual(result['totalMatches'],2)
        self.assertEqual(api.instrument('510300.SS')['name'],'沪深300ETF华泰柏瑞')

    def test_extended_search_fields_never_enter_persistent_watchlist_schema(self):
        self.assertEqual(set(api.instrument('512480.SS')), {'symbol','code','name'})
        added = api.update_watchlist({'action':'add','symbol':'512480','expectedRevision':0}, server.normalize_symbol)
        self.assertEqual(set(added['items'][0]), {'symbol','code','name','addedAt'})
        self.assertEqual(api.get_watchlist(server.normalize_symbol), added)
        self.assertEqual(added['items'][0]['name'], '半导体ETF')

    def test_history_presentation_copy_does_not_modify_original_name_or_bars(self):
        source = {'bars':[{'close':1}], 'meta':{'symbol':'600186.SS','name':'Original Yahoo Name'}}
        saved=copy.deepcopy(source);value=api.enrich_history(source)
        self.assertEqual(value['meta']['nameZh'],'XD莲花控')
        self.assertEqual(value['meta']['originalName'],'Original Yahoo Name')
        self.assertEqual(source,saved)

    def opener(self, count=102, mutate=None):
        rows=etf_rows(count);calls=[]
        def open_request(request, timeout):
            calls.append(request.full_url)
            query=parse_qs(urlparse(request.full_url).query)
            if 'StockCount' in request.full_url:
                value=str(count)
            else:
                page=int(query['page'][0]);value=rows[(page-1)*100:page*100]
            if mutate: value=mutate(request.full_url,value,len(calls))
            return Response(value)
        return open_request,calls

    def test_refresh_paginates_checks_count_unique_and_preserves_raw(self):
        opener,calls=self.opener()
        result=catalog.refresh_etfs(self.etfs,force=True,now=NOW,opener=opener)
        self.assertTrue(result['ok']);self.assertEqual(result['total'],102)
        self.assertEqual(len(calls),4)
        data=json.loads(self.etfs.read_text(encoding='utf-8'));self.assertEqual(len(data['items']),102)
        raw=self.etfs.parent/'raw'/result['rawRun']
        self.assertEqual(len(list(raw.glob('*.raw'))),4)
        manifest=json.loads((raw/'manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['ok']);self.assertEqual(len(manifest['responses']),4)

    def test_incomplete_or_duplicate_page_and_changed_total_never_replace_cache(self):
        def missing(url,value,n): return value[:-1] if 'getHQNodeData?' in url and 'page=1&' in url else value
        def duplicate(url,value,n):
            if 'getHQNodeData?' in url and 'page=2&' in url: value[0]=etf_rows(1)[0]
            return value
        def changed(url,value,n): return '103' if 'StockCount' in url and n>1 else value
        before=self.etfs.read_bytes()
        for mutate in [missing,duplicate,changed]:
            opener,_=self.opener(mutate=mutate)
            result=catalog.refresh_etfs(self.etfs,force=True,now=NOW,opener=opener)
            self.assertFalse(result['ok']);self.assertEqual(self.etfs.read_bytes(),before)

    def test_refresh_timeout_retains_old_date_and_search_marks_stale(self):
        before=self.etfs.read_bytes()
        result=catalog.refresh_etfs(self.etfs,force=True,now=NOW,opener=lambda *a,**k:(_ for _ in ()).throw(TimeoutError('mock timeout')))
        self.assertFalse(result['ok']);self.assertEqual(result['retainedSnapshotDate'],'2026-11-05')
        self.assertEqual(self.etfs.read_bytes(),before)
        search=api.search_instruments('红利')
        self.assertEqual(search['totalMatches'],2)
        self.assertEqual(search['coverage']['etfs']['status'],'stale')
        self.assertIn('timeout',search['coverage']['etfs']['lastError'])

    def test_refresh_fresh_24h_cache_never_calls_network(self):
        result=catalog.refresh_etfs(self.etfs,now=NOW,opener=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
        self.assertTrue(result['ok']);self.assertEqual(result['cache'],'fresh')

    def test_atomic_cache_write_failure_preserves_previous_bytes(self):
        before=self.etfs.read_bytes();original=catalog._atomic_json;opener,_=self.opener()
        def guarded(path,value):
            if Path(path)==self.etfs: raise OSError('mock cache write failure')
            return original(path,value)
        with patch.object(catalog,'_atomic_json',side_effect=guarded):
            result=catalog.refresh_etfs(self.etfs,force=True,now=NOW,opener=opener)
        self.assertFalse(result['ok']);self.assertEqual(self.etfs.read_bytes(),before)

    def test_status_log_failure_after_publication_does_not_claim_old_cache(self):
        original=catalog._atomic_json;opener,_=self.opener()
        def guarded(path,value):
            if Path(path).name=='refresh-status.json': raise OSError('mock status write failure')
            return original(path,value)
        with patch.object(catalog,'_atomic_json',side_effect=guarded):
            result=catalog.refresh_etfs(self.etfs,force=True,now=NOW,opener=opener)
        self.assertTrue(result['ok']);self.assertIn('warning',result)
        self.assertEqual(len(catalog.validate_etf_cache(json.loads(self.etfs.read_text(encoding='utf-8')))),102)

    def test_old_directory_is_explicitly_stale_without_rewriting_it(self):
        old=cache_doc(self.etf_doc['items'], NOW-timedelta(days=2))
        self.etfs.write_text(json.dumps(old),encoding='utf-8');before=self.etfs.read_bytes()
        result=api.search_instruments('证券')
        self.assertEqual(result['coverage']['etfs']['status'],'stale')
        self.assertEqual(result['snapshotDate'],'2026-11-03')
        self.assertEqual(self.etfs.read_bytes(),before)

    def test_real_http_contract_reads_local_directory_only(self):
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            conn.request('GET','/api/instruments?query='+quote('红利'))
            response=conn.getresponse();value=json.loads(response.read());conn.close()
            self.assertEqual(response.status,200);self.assertEqual(value['totalMatches'],2)
            self.assertEqual(value['coverage']['etfs']['count'],4)
        finally:
            httpd.shutdown();httpd.server_close();thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
