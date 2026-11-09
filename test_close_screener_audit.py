"""Offline tests for close-aware reporting and cross-midnight evidence."""
from datetime import datetime
import unittest
from unittest.mock import patch

import audit_daily_report as audit
from market_clock import CN, market_status


class CloseAuditTests(unittest.TestCase):
    def calendar(self, moment):
        status=market_status(moment)
        return {'targetDate':status['targetDate'], 'targetPolicy':'cn_close_v1',
                'evaluatedAt':moment.isoformat(), 'asOf':moment.date().isoformat(),
                'latestCompletedSession':status['targetDate'], 'dateVerified':True,
                'latestObservedTradingDate':status['targetDate'], 'shouldRun':True}

    def test_evening_target_is_same_day_and_remains_current_after_midnight(self):
        recorded=datetime(2026,11,4,15,1,tzinfo=CN)
        calendar=self.calendar(recorded)
        for now in [recorded,datetime(2026,11,5,0,1,tzinfo=CN)]:
            with patch.object(audit,'now_cn',return_value=now):
                self.assertEqual(audit.validate_calendar(calendar,'2026-11-04')[1],'2026-11-04')

    def test_previous_day_report_is_rejected_after_new_close(self):
        calendar=self.calendar(datetime(2026,11,4,15,1,tzinfo=CN))
        with patch.object(audit,'now_cn',return_value=datetime(2026,11,5,15,1,tzinfo=CN)):
            with self.assertRaisesRegex(ValueError,'differs'):
                audit.validate_calendar(calendar,'2026-11-04')

    def test_intraday_same_day_target_is_rejected(self):
        calendar=self.calendar(datetime(2026,11,4,14,59,tzinfo=CN))
        calendar.update(targetDate='2026-11-04',latestCompletedSession='2026-11-04',latestObservedTradingDate='2026-11-04')
        with self.assertRaisesRegex(ValueError,'latest completed'):
            audit.validate_calendar(calendar,'2026-11-04','2026-11-04')

    def test_prior_evening_source_is_valid_for_next_morning_report(self):
        meta={'fetchedAt':'2026-11-04T15:10:00+08:00','completenessPolicy':'cn_close_v1',
              'readiness':'ready','expectedThrough':'2026-11-04','sourceSessionConfirmed':True,
              'sourceRegularMarketTime':datetime(2026,11,4,15,tzinfo=CN).timestamp()}
        original=dict(meta)
        result=audit.validate_source_completion(meta,'2026-11-04',datetime(2026,11,5,0,1,tzinfo=CN))
        self.assertEqual(result.date().isoformat(),'2026-11-04')
        self.assertEqual(meta,original)

    def test_preclose_source_or_unconfirmed_source_cannot_pass_audit(self):
        meta={'fetchedAt':'2026-11-04T15:10:00+08:00','completenessPolicy':'cn_close_v1',
              'readiness':'ready','expectedThrough':'2026-11-04','sourceSessionConfirmed':True,
              'sourceRegularMarketTime':datetime(2026,11,4,15,tzinfo=CN).timestamp()}
        for changes in [{'fetchedAt':'2026-11-04T14:59:59+08:00'},
                        {'sourceSessionConfirmed':False}, {'readiness':'awaiting_source'},
                        {'completenessPolicy':'exclude_exchange_today'}]:
            with self.assertRaises(ValueError):
                audit.validate_source_completion({**meta,**changes},'2026-11-04',datetime(2026,11,5,0,1,tzinfo=CN))


if __name__=='__main__':
    unittest.main(verbosity=2)
