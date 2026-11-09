"""Diagnostics must warn without weakening the existing fail-closed calendar."""
from datetime import datetime
import unittest
from unittest.mock import patch
import market_clock as clock
import server


class CalendarNoticeTests(unittest.TestCase):
    def messages(self, value):
        self.assertTrue(hasattr(clock, 'calendar_warnings'), 'calendar expiry diagnostics are missing')
        return clock.calendar_warnings(datetime.fromisoformat(value))

    def test_warns_before_unverified_year_without_changing_target(self):
        before = clock.market_status(datetime.fromisoformat('2026-11-06T15:00:00+08:00'))
        warnings = self.messages('2026-11-06T15:00:00+08:00')
        self.assertTrue(any('2027' in x and '56' in x for x in warnings))
        self.assertEqual(before, clock.market_status(datetime.fromisoformat('2026-11-06T15:00:00+08:00')))

    def test_no_warning_when_next_year_outside_notice_window(self):
        self.assertEqual(self.messages('2026-06-01T15:00:00+08:00'), [])

    def test_unknown_current_year_warns_and_still_rejects_trading_guess(self):
        warnings = self.messages('2027-01-05T15:00:00+08:00')
        self.assertTrue(any('2027' in x and '禁止' in x for x in warnings))
        with self.assertRaises(clock.CalendarError):
            clock.market_status(datetime.fromisoformat('2027-01-05T15:00:00+08:00'))

    def test_previous_year_needed_for_completed_session_is_warned(self):
        warnings = self.messages('2026-01-01T00:01:00+08:00')
        self.assertTrue(any('2025' in x for x in warnings))

    def test_beijing_date_controls_notice_window(self):
        self.assertEqual(self.messages('2026-11-05T16:00:00+00:00'),
                         self.messages('2026-11-06T00:00:00+08:00'))

    def test_server_logs_warnings_before_binding_without_hiding_service(self):
        with patch.object(server, 'calendar_warnings', create=True, return_value=['测试日历到期警告']), \
             patch.object(server, '_calendar_notice_day', None, create=True), \
             patch.object(server, 'ThreadingHTTPServer') as factory, \
             patch('sys.argv', ['server.py', '--port', '18766']):
            with self.assertLogs('chanxu.calendar', level='WARNING') as captured:
                server.main()
            self.assertTrue(any('测试日历到期警告' in line for line in captured.output))
            factory.assert_called_once_with(('127.0.0.1', 18766), server.Handler)
            factory.return_value.serve_forever.assert_called_once()
            factory.return_value.server_close.assert_called_once()

    def test_long_running_server_rechecks_on_beijing_date_change(self):
        self.assertTrue(hasattr(server, 'log_calendar_warnings'), 'server daily calendar warning is missing')
        with patch.object(server, '_calendar_notice_day', None), \
             patch.object(server, 'calendar_warnings', return_value=['年界警告']) as warnings:
            with self.assertLogs('chanxu.calendar', level='WARNING') as captured:
                server.log_calendar_warnings(datetime.fromisoformat('2026-12-31T16:00:00+08:00'))
                server.log_calendar_warnings(datetime.fromisoformat('2026-12-31T23:59:00+08:00'))
                server.log_calendar_warnings(datetime.fromisoformat('2027-01-01T00:01:00+08:00'))
            self.assertEqual(warnings.call_count, 2)
            self.assertEqual(len(captured.output), 2)


if __name__ == '__main__':
    unittest.main()
