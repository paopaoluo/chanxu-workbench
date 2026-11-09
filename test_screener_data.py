import unittest
from datetime import date, datetime
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import screener_data as sd


class UniverseTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(sd.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def stock(self, symbol, name="普通股票"):
        return sd.classify_stock({"symbol": symbol, "code": symbol[2:], "name": name})

    def test_mainboard_kept_st_excluded(self):
        for symbol in ("sh600000", "sh601398", "sh603000", "sh605001", "sz000001", "sz001001", "sz002001", "sz003001"):
            stock, reason = self.stock(symbol)
            self.assertIsNotNone(stock)
            self.assertIsNone(reason)
        for name in ("ST某公司", "*ST某公司", "st某公司", "S*ST某公司"):
            self.assertIsNone(self.stock("sh600000", name)[0])
            self.assertEqual(self.stock("sh600000", name)[1], "ST或*ST")

    def test_only_requested_boards_excluded(self):
        for symbol in ("sh688001", "sh689009", "sz300001", "sz301001"):
            self.assertIsNone(self.stock(symbol)[0])

    def test_nonstocks_not_admitted(self):
        for symbol in ("sh510300", "sh900901", "sz200001", "sh000001", "sz159001", "hk00700"):
            self.assertIsNone(self.stock(symbol)[0])

    def test_beijing_excluded(self):
        for symbol in ("bj920000", "bj830001", "bj870001", "bj430001"):
            stock, reason = self.stock(symbol)
            self.assertIsNone(stock)
            self.assertEqual(reason, "北交所")

    def test_old_same_day_cache_cannot_bypass_new_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.json"
            path.write_text(json.dumps({"snapshotDate": sd.now_cn().date().isoformat(), "complete": True,
                                        "stocks": [{"code": "920000", "exchange": "BJ"}]}), encoding="utf-8")
            with patch.object(sd, "UNIVERSE_PATH", path), patch.object(sd, "_json_get", side_effect=sd.DataError("fresh fetch required")):
                with self.assertRaisesRegex(sd.DataError, "fresh fetch required"):
                    sd.load_universe()

    def test_holiday_and_makeup_weekend(self):
        for d in ("2026-10-01", "2026-10-07", "2026-10-10", "2026-02-23", "2026-01-04"):
            self.assertFalse(sd.is_trading_day(d))
        self.assertTrue(sd.is_trading_day("2026-10-08"))
        with self.assertRaises(sd.DataError):
            sd.is_trading_day("2027-01-04")

    @patch.object(sd, "get_history")
    def test_target_after_holiday(self, history):
        history.return_value = {"bars": [{"time": "2026-09-30"}], "meta": {"stale": False, "readiness": "ready"}}
        result = sd.resolve_target_date("2026-10-08")
        self.assertEqual(result["targetDate"], "2026-09-30")
        self.assertTrue(result["shouldRun"])

    @patch.object(sd, "get_history")
    def test_stale_or_behind_never_fallback(self, history):
        history.return_value = {"bars": [{"time": "2026-11-02"}], "meta": {"stale": False, "readiness": "ready"}}
        with self.assertRaises(sd.DataError):
            sd.resolve_target_date("2026-11-04")
        history.return_value["meta"]["stale"] = True
        with self.assertRaises(sd.DataError):
            sd.resolve_target_date("2026-11-03")

    @patch.object(sd, "get_history")
    def test_intraday_then_at_close_and_after_midnight(self, history):
        for moment, expected in [
            (datetime(2026,11,4,14,59,59,tzinfo=sd.CN), '2026-11-03'),
            (datetime(2026,11,4,15,0,0,tzinfo=sd.CN), '2026-11-04'),
            (datetime(2026,11,4,23,59,59,tzinfo=sd.CN), '2026-11-04'),
            (datetime(2026,11,5,0,1,0,tzinfo=sd.CN), '2026-11-04'),
            (date(2026,11,5), '2026-11-04'),
        ]:
            history.return_value={'bars':[{'time':expected}], 'meta':{'stale':False,'readiness':'ready'}}
            result=sd.resolve_target_date(moment)
            self.assertEqual(result['targetDate'],expected)
            self.assertEqual(result['latestCompletedSession'],expected)
            self.assertEqual(result['targetPolicy'],'cn_close_v1')
            self.assertIn('T',result['evaluatedAt'])

    @patch.object(sd, "get_history")
    def test_default_clock_preserves_actual_time_of_day(self, history):
        history.return_value={'bars':[{'time':'2026-11-04'}], 'meta':{'stale':False,'readiness':'ready'}}
        with patch.object(sd,'now_cn',return_value=datetime(2026,11,4,15,1,tzinfo=sd.CN)):
            self.assertEqual(sd.resolve_target_date()['targetDate'],'2026-11-04')

    @patch.object(sd, "get_history")
    def test_awaiting_source_cannot_confirm_even_if_target_bar_is_present(self, history):
        history.return_value={'bars':[{'time':'2026-11-04'}], 'meta':{'stale':False,'readiness':'awaiting_source'}}
        with self.assertRaisesRegex(sd.DataError,'尚未更新'):
            sd.resolve_target_date(datetime(2026,11,4,15,1,tzinfo=sd.CN))
        self.assertEqual(history.call_count,3)

    @patch.object(sd, "get_history")
    def test_holiday_does_not_fetch_or_republish_previous_day(self, history):
        result=sd.resolve_target_date('2026-11-07T18:00:00+08:00')
        self.assertEqual(result['targetDate'],'2026-11-06')
        self.assertFalse(result['shouldRun'])
        self.assertFalse(result['dateVerified'])
        history.assert_not_called()

    @patch.object(sd, "get_history")
    def test_index_network_failure_retries_without_old_date_fallback(self, history):
        history.side_effect=[sd.DataError('temporary'), {'bars':[{'time':'2026-11-04'}], 'meta':{'stale':False,'readiness':'ready'}}]
        self.assertEqual(sd.resolve_target_date('2026-11-05')['targetDate'],'2026-11-04')
        self.assertEqual(history.call_count,2)


if __name__ == "__main__":
    unittest.main()
