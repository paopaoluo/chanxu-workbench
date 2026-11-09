"""Offline regression checks. No Yahoo requests, accounts or existing cache writes."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import server


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.payload).encode()


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 30, 20, tzinfo=timezone.utc).astimezone(tz or timezone.utc)


def fixture():
    days = [datetime(2026, 10, 23, tzinfo=timezone.utc) + timedelta(days=i) for i in range(9)]
    return {"chart": {"result": [{"meta": {"exchangeTimezoneName": "UTC", "currency": "USD", "shortName": "Offline fixture"},
                                  "timestamp": [int(d.timestamp()) for d in days],
                                  "indicators": {"quote": [{"open": [10]*9, "high": [12]*9, "low": [9]*9,
                                                             "close": [11]*9, "volume": [100]*9}]}}]}}


def cache_payload():
    """Shape returned by _fetch, not malformed/legacy raw cache content."""
    days = ['2026-10-23', '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29']
    return {'bars': [{'time': day, 'open': 10, 'high': 12, 'low': 9, 'close': 11, 'volume': 100}
                     for day in days],
            'meta': {'symbol': 'AAPL', 'currency': 'USD', 'exchangeTimezone': 'UTC',
                     'exchangeOffsetSeconds': 0, 'source': 'Yahoo Finance chart API',
                     'range': '2y', 'interval': '1d', 'asOf': days[-1], 'completeThrough': days[-1],
                     'adjustment': 'unadjusted OHLC; adjusted close is not applied',
                     'fetchedAt': FixedDateTime.now(timezone.utc).isoformat(), 'stale': False,
                     'completenessPolicy': 'exclude_exchange_today'}}


class ServerRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(server, "CACHE_DIR", Path(self.temp.name))
        self.cache_patch.start()
    def tearDown(self):
        self.cache_patch.stop()
        self.temp.cleanup()

    def test_symbol_validation(self):
        self.assertEqual(server.normalize_symbol("510300"), "510300.SS")
        self.assertEqual(server.normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(server.normalize_symbol("aapl"), "AAPL")
        for invalid in ["", "贵州茅台", "http://example.test", "../cache", "AAPL?x=1"]:
            with self.assertRaises(server.DataError):
                server.normalize_symbol(invalid)

    def test_daily_policy_currency_and_finite_numbers(self):
        data = fixture()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = float("nan")
        with patch.object(server, "datetime", FixedDateTime), patch.object(server, "urlopen", return_value=Response(data)):
            result = server._fetch("AAPL", "2y")
        self.assertEqual(result["bars"][-1]["time"], "2026-10-29")
        self.assertEqual(len(result["bars"]), 6)
        self.assertEqual(result["meta"]["currency"], "USD")
        self.assertEqual(result["meta"]["completenessPolicy"], "exclude_exchange_today")
        self.assertIn("非A股", result["meta"]["completenessNote"])
        self.assertIn("排除交易所当地日期为今天的日线", result["meta"]["completenessNote"])

    def test_fresh_cache_metadata_and_atomic_file(self):
        payload = cache_payload()
        cached_at = FixedDateTime.now(timezone.utc).timestamp()
        with patch.object(server, "_fetch", return_value=payload), patch.object(server.time, "time", return_value=cached_at), patch.object(server, "datetime", FixedDateTime):
            online = server.get_history("AAPL", "2y", True)
        self.assertEqual(online["meta"]["cache"], "network")
        with patch.object(server, "_fetch", side_effect=AssertionError("must use cache")), patch.object(server.time, "time", return_value=cached_at+60), patch.object(server, "datetime", FixedDateTime):
            cached = server.get_history("AAPL", "2y")
        self.assertEqual(cached["meta"]["cacheAgeSeconds"], 60)
        self.assertFalse(cached["meta"]["stale"])
        self.assertEqual(cached["meta"]["currency"], "USD")
        self.assertTrue(cached["meta"]["fetchedAt"])
        self.assertEqual(cached['meta']['fetchedAt'], payload['meta']['fetchedAt'])
        self.assertEqual(cached['bars'], payload['bars'])
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_stale_cache_is_explicit(self):
        p = server._cache_path("AAPL", "2y")
        payload = cache_payload()
        cached_at = FixedDateTime.now(timezone.utc).timestamp()
        p.write_text(json.dumps({"cachedAt": cached_at, "payload": payload}), encoding="utf8")
        with patch.object(server, "_fetch", side_effect=server.DataError("offline")), patch.object(server.time, "time", return_value=cached_at+1500), patch.object(server, "datetime", FixedDateTime):
            result = server.get_history("AAPL", "2y", True)
        self.assertTrue(result["meta"]["stale"])
        self.assertEqual(result["meta"]["cacheAgeSeconds"], 1500)
        self.assertIn("保留上次数据", result["meta"]["staleReason"])
        self.assertEqual(result["meta"]["completenessPolicy"], "exclude_exchange_today")
        self.assertEqual(result['meta']['fetchedAt'], payload['meta']['fetchedAt'])
        self.assertEqual(result['bars'], payload['bars'])

    def test_network_error_without_cache(self):
        with patch.object(server, "_fetch", side_effect=server.DataError("offline")):
            with self.assertRaises(server.DataError):
                server.get_history("AAPL", "2y", True)
        with self.assertRaises(server.DataError):
            server.get_history("AAPL", "1m")

    def test_http_routes_do_not_expose_arbitrary_files(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urlopen(root + "/health", timeout=5) as result:
                self.assertTrue(json.load(result)["ok"])
            with urlopen(root + "/app.js", timeout=5) as result:
                self.assertEqual(result.status, 200)
            for url in ["/server.py", "/cache/private.json", "/../server.py"]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(root + url, timeout=5)
                self.assertEqual(error.exception.code, 404)
            with patch.object(server, "get_history", side_effect=server.DataError("请核对代码")):
                with self.assertRaises(HTTPError) as error:
                    urlopen(root + "/api/history?symbol=bad", timeout=5)
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(json.load(error.exception)["error"], "请核对代码")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
