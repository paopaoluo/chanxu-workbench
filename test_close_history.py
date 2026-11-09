import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
import server
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

CN=timezone(timedelta(hours=8))
def dt(value): return datetime.fromisoformat(value)
def response(regular='2026-11-04T15:00:00+08:00'):
    days=['2026-10-28','2026-10-29','2026-10-30','2026-11-02','2026-11-03','2026-11-04']
    return {'chart':{'result':[{'meta':{'symbol':'600000.SS','exchangeTimezoneName':'Asia/Shanghai',
        'regularMarketTime':int(dt(regular).timestamp())},
        'timestamp':[int(dt(day+'T09:30:00+08:00').timestamp()) for day in days],
        'indicators':{'quote':[{'open':[10]*6,'high':[12]*6,'low':[9]*6,'close':[11]*6,'volume':[100]*6}]}}]}}

def fetch_at(now, regular='2026-11-04T15:00:00+08:00'):
    with patch.object(server,'_utc_now',return_value=dt(now)),patch.object(server,'urlopen',return_value=io.BytesIO(json.dumps(response(regular)).encode())):
        return server._fetch('600000.SS','2y')

class CloseHistoryTests(unittest.TestCase):
    def close_missing(self, regular, price):
        source=response(regular)
        source['chart']['result'][0]['indicators']['quote'][0]['close'][-1]=None
        source['chart']['result'][0]['meta']['regularMarketPrice']=price
        with patch.object(server,'_utc_now',return_value=dt('2026-11-05T00:07:00+08:00')),patch.object(server,'urlopen',return_value=io.BytesIO(json.dumps(source).encode())):
            return server._fetch('600000.SS','2y')

    def test_missing_daily_close_uses_same_response_closed_quote(self):
        data=self.close_missing('2026-11-04T15:00:00+08:00',11.5)
        self.assertEqual(data['meta']['asOf'],'2026-11-04')
        self.assertEqual(data['bars'][-1]['close'],11.5)
        self.assertEqual(data['meta']['closeFieldFallback'][0]['source'],'meta.regularMarketPrice')

    def test_missing_close_does_not_use_intraday_quote(self):
        data=self.close_missing('2026-11-04T14:59:59+08:00',11.5)
        self.assertEqual(data['meta']['asOf'],'2026-11-03')
        self.assertEqual(data['meta']['readiness'],'awaiting_source')

    def test_missing_close_does_not_use_other_days_quote(self):
        data=self.close_missing('2026-11-03T15:00:00+08:00',11.5)
        self.assertEqual(data['meta']['asOf'],'2026-11-03')

    def test_missing_close_rejects_out_of_range_or_nonfinite_quote(self):
        for price in (12.5,8.5,0,None,float('nan')):
            with self.subTest(price=price):
                data=self.close_missing('2026-11-04T15:00:00+08:00',price)
                self.assertEqual(data['meta']['asOf'],'2026-11-03')

    def test_real_index_float32_rounding_is_clamped_with_provenance(self):
        source=response()
        result=source['chart']['result'][0]
        q=result['indicators']['quote'][0]
        q['open'][-1]=3670
        q['low'][-1]=3660
        q['high'][-1]=3686.172607421875
        q['close'][-1]=None
        result['meta']['regularMarketPrice']=3686.173
        with patch.object(server,'_utc_now',return_value=dt('2026-11-05T00:07:00+08:00')),patch.object(server,'urlopen',return_value=io.BytesIO(json.dumps(source).encode())):
            data=server._fetch('000001.SS','6mo')
        self.assertEqual(data['meta']['asOf'],'2026-11-04')
        self.assertEqual(data['bars'][-1]['high'],3686.172607421875)
        self.assertEqual(data['bars'][-1]['close'],3686.172607421875)
        proof=data['meta']['closeFieldFallback'][0]
        self.assertEqual(proof['reportedQuoteValue'],3686.173)
        self.assertEqual(proof['value'],data['bars'][-1]['close'])
        self.assertAlmostEqual(proof['roundingAdjustment'],-0.000392578125193,places=12)
        self.assertEqual(proof['tolerance'],0.0005)

    def test_rounding_tolerance_does_not_admit_meaningful_out_of_range_price(self):
        self.assertEqual(self.close_missing('2026-11-04T15:00:00+08:00',12.001)['meta']['asOf'],'2026-11-03')

    def test_intraday_excludes_today(self):
        data=fetch_at('2026-11-04T14:59:59+08:00')
        self.assertEqual(data['meta']['asOf'],'2026-11-03')
        self.assertEqual(data['meta']['expectedThrough'],'2026-11-03')

    def test_close_includes_today_with_source_confirmation(self):
        data=fetch_at('2026-11-04T15:00:00+08:00')
        self.assertEqual(data['meta']['asOf'],'2026-11-04')
        self.assertEqual(data['meta']['readiness'],'ready')
        self.assertTrue(data['meta']['sourceSessionConfirmed'])

    def test_close_waits_for_source_timestamp(self):
        data=fetch_at('2026-11-04T15:01:00+08:00','2026-11-04T14:59:00+08:00')
        self.assertEqual(data['meta']['asOf'],'2026-11-03')
        self.assertEqual(data['meta']['expectedThrough'],'2026-11-04')
        self.assertEqual(data['meta']['readiness'],'awaiting_source')

    def test_midnight_works_without_zoneinfo_and_missing_offset(self):
        with patch.object(server,'ZoneInfo',None):
            data=fetch_at('2026-11-04T16:07:00+00:00')
        self.assertEqual(data['meta']['asOf'],'2026-11-04')
        self.assertEqual(data['meta']['readiness'],'ready')

    def test_future_quote_timestamp_cannot_confirm_same_day(self):
        data=fetch_at('2026-11-04T15:01:00+08:00','2026-11-05T15:00:00+08:00')
        self.assertEqual(data['meta']['asOf'],'2026-11-03')
        self.assertEqual(data['meta']['readiness'],'awaiting_source')

    def test_non_cn_policy_remains_conservative(self):
        with patch.object(server,'_utc_now',return_value=dt('2026-11-04T18:00:00+08:00')),patch.object(server,'urlopen',return_value=io.BytesIO(json.dumps(response()).encode())):
            data=server._fetch('AAPL','2y')
        self.assertEqual(data['meta']['asOf'],'2026-11-03')
        self.assertEqual(data['meta']['completenessPolicy'],'exclude_exchange_today')

    def test_http_clock_endpoint_has_actual_cn_clock_fields(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        try:
            with patch.object(server,'_utc_now',return_value=dt('2026-11-05T00:07:00+08:00')):
                with urlopen(f'http://127.0.0.1:{http.server_port}/api/market-status') as result:
                    status=json.load(result)
            self.assertEqual(status['targetDate'],'2026-11-04')
            self.assertEqual(status['phase'],'preopen')
            self.assertEqual(status['policy'],'cn_close_v1')
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)

    def test_cache_before_close_does_not_hide_today(self):
        before=fetch_at('2026-11-04T14:59:00+08:00')
        after=fetch_at('2026-11-04T15:00:00+08:00')
        self.check_refetch(before,after,'2026-11-04T15:00:00+08:00')

    def test_old_policy_cache_must_refetch(self):
        old=fetch_at('2026-11-04T15:00:00+08:00')
        old['meta']['completenessPolicy']='exclude_exchange_today'
        new=fetch_at('2026-11-04T15:01:00+08:00')
        self.check_refetch(old,new,'2026-11-04T15:01:00+08:00')

    def test_pending_cache_must_refetch(self):
        old=fetch_at('2026-11-04T15:01:00+08:00','2026-11-04T14:59:00+08:00')
        new=fetch_at('2026-11-04T15:02:00+08:00')
        self.check_refetch(old,new,'2026-11-04T15:02:00+08:00')

    def test_midnight_cache_must_refetch_even_when_target_same(self):
        old=fetch_at('2026-11-04T23:59:50+08:00')
        new=fetch_at('2026-11-05T00:00:01+08:00')
        self.check_refetch(old,new,'2026-11-05T00:00:01+08:00')

    def check_refetch(self, old, new, now):
        with tempfile.TemporaryDirectory() as temp, patch.object(server,'CACHE_DIR',Path(temp)),patch.object(server,'_utc_now',return_value=dt(now)):
            path=server._cache_path('600000.SS','2y')
            path.write_text(json.dumps({'cachedAt':dt(now).timestamp()-1,'payload':old}),encoding='utf8')
            with patch.object(server,'time') as tm,patch.object(server,'_fetch',return_value=new) as fetch:
                tm.time.return_value=dt(now).timestamp()
                result=server.get_history('600000.SS','2y')
            fetch.assert_called_once()
            self.assertEqual(result['meta']['asOf'],'2026-11-04')
            self.assertEqual(result['meta']['cache'],'network')

    def test_stale_fallback_preserves_origin_and_is_not_ready(self):
        old=fetch_at('2026-11-04T14:59:00+08:00')
        old['meta']['completenessPolicy']='exclude_exchange_today'
        old_fetched=old['meta']['fetchedAt']
        with tempfile.TemporaryDirectory() as temp, patch.object(server,'CACHE_DIR',Path(temp)),patch.object(server,'_utc_now',return_value=dt('2026-11-04T15:01:00+08:00')):
            server._cache_path('600000.SS','2y').write_text(json.dumps({'cachedAt':dt('2026-11-04T14:59:00+08:00').timestamp(),'payload':old}),encoding='utf8')
            with patch.object(server,'_fetch',side_effect=server.DataError('offline')):
                result=server.get_history('600000.SS','2y',refresh=True)
        self.assertTrue(result['meta']['stale'])
        self.assertEqual(result['meta']['readiness'],'awaiting_source')
        self.assertEqual(result['meta']['fetchedAt'],old_fetched)
        self.assertEqual(result['meta']['cachedCompletenessPolicy'],'exclude_exchange_today')
        self.assertEqual(result['meta']['asOf'],'2026-11-03')

    def test_current_complete_cache_is_reused(self):
        data=fetch_at('2026-11-04T15:05:00+08:00')
        with tempfile.TemporaryDirectory() as temp,patch.object(server,'CACHE_DIR',Path(temp)),patch.object(server,'_utc_now',return_value=dt('2026-11-04T15:06:00+08:00')):
            server._cache_path('600000.SS','2y').write_text(json.dumps({'cachedAt':dt('2026-11-04T15:05:00+08:00').timestamp(),'payload':data}),encoding='utf8')
            with patch.object(server,'time') as tm,patch.object(server,'_fetch') as fetch:
                tm.time.return_value=dt('2026-11-04T15:06:00+08:00').timestamp()
                result=server.get_history('600000.SS','2y')
            fetch.assert_not_called()
            self.assertEqual(result['meta']['readiness'],'ready')


if __name__=='__main__': unittest.main()
