import unittest
from datetime import datetime, timezone
import market_clock as clock


class MarketClockTests(unittest.TestCase):
    def test_boundaries_and_midnight(self):
        cases = [
            ('2026-11-04T00:01:00+08:00','preopen','2026-11-03'),
            ('2026-11-04T09:29:59+08:00','preopen','2026-11-03'),
            ('2026-11-04T09:30:00+08:00','morning','2026-11-03'),
            ('2026-11-04T11:30:00+08:00','lunch','2026-11-03'),
            ('2026-11-04T13:00:00+08:00','afternoon','2026-11-03'),
            ('2026-11-04T14:59:59+08:00','afternoon','2026-11-03'),
            ('2026-11-04T15:00:00+08:00','closed','2026-11-04'),
            ('2026-11-04T23:59:59+08:00','closed','2026-11-04'),
            ('2026-11-05T00:00:00+08:00','preopen','2026-11-04'),
            ('2026-11-07T12:00:00+08:00','non_trading_day','2026-11-06'),
            ('2026-10-01T16:00:00+08:00','non_trading_day','2026-09-30'),
            ('2026-10-08T14:00:00+08:00','afternoon','2026-09-30'),
            ('2026-10-08T15:00:00+08:00','closed','2026-10-08'),
        ]
        for value, phase, target in cases:
            with self.subTest(value=value):
                status=clock.market_status(datetime.fromisoformat(value))
                self.assertEqual(status['phase'],phase)
                self.assertEqual(status['targetDate'],target)
                self.assertEqual(status['timezone'],'Asia/Shanghai')
                self.assertTrue(status['serverTime'].endswith('+08:00'))

    def test_utc_is_converted_not_used_as_local_date(self):
        status=clock.market_status(datetime(2026,11,4,16,7,tzinfo=timezone.utc))
        self.assertEqual(status['targetDate'],'2026-11-04')
        self.assertTrue(status['serverTime'].startswith('2026-11-05T00:07'))

    def test_makeup_weekend_closed(self):
        self.assertFalse(clock.is_trading_day('2026-10-10'))

    def test_unknown_year_fails_closed(self):
        with self.assertRaises(clock.CalendarError):
            clock.market_status(datetime.fromisoformat('2027-01-05T16:00:00+08:00'))

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            clock.market_status(datetime(2026,11,4,15))


if __name__=='__main__': unittest.main()
