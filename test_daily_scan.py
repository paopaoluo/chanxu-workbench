"""Offline orchestration review tests. All market and engine output are mocks."""
import contextlib
import gzip
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import daily_scan as scan


TARGET = '2026-11-03'
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'engine.js').write_text('fixture-only-engine', encoding='utf-8')
        for name in ('daily_scan_engine.js', 'screener_data.py', 'server.py', 'daily_scan.py', 'market_clock.py'):
            (self.root / name).write_text('fixture-only-' + name, encoding='utf-8')
        self.engine_hash = scan.sha((self.root / 'engine.js').read_bytes())
        self.stocks = [
            dict(code='600001', name='模拟甲', symbol='600001.SS', exchange='SH', board='沪市主板', st=False),
            dict(code='000001', name='模拟乙', symbol='000001.SZ', exchange='SZ', board='深市主板', st=False),
            # Unknown future history-provider gap, not an intentionally excluded board.
            dict(code='600099', name='模拟未覆盖', symbol=None, exchange='SH', board='沪市主板', st=False, coverageNote='unsupported fixture'),
        ]
        self.universe = dict(stocks=self.stocks, total=3, sourceTotal=5, source='MOCK ONLY', eligibleHistoryCount=2,
                             unsupportedHistoryCount=1, universePolicy='sh_sz_main_no_st_v2')
        self.bars = [dict(time='2026-11-02', open=10, high=11, low=9, close=10, volume=100),
                     dict(time=TARGET, open=10, high=11, low=9, close=10, volume=100)]
        self.payload = dict(bars=self.bars, meta=dict(stale=False, source='MOCK ONLY',
            readiness='ready', completenessPolicy='cn_close_v1', expectedThrough=TARGET,
            fetchedAt=TARGET+'T16:00:00+08:00', sourceSessionConfirmed=True,
            sourceRegularMarketTime=datetime.fromisoformat(TARGET+'T15:00:00+08:00').timestamp()))
        self.fetch_calls = []
        self.bridge_calls = []
        owner = self

        class FakeBridge:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def analyze(self, stock, bars, target):
                owner.bridge_calls.append((stock['code'], bars, target))
                return dict(code=stock['code'], status='ok', asOf=target, targetDate=target,
                            barCount=len(bars), close=10, signals=[], engineSha256=owner.engine_hash, ruleVersion='MOCK ONLY')

        def fetch(stock, force=False):
            self.fetch_calls.append(stock['code'])
            return self.payload, None

        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(scan, 'ROOT', self.root))
        self.stack.enter_context(patch.object(scan, 'REPORTS', self.root / 'reports'))
        self.stack.enter_context(patch.object(scan, 'now_cn', return_value=NOW))
        self.stack.enter_context(patch.object(scan, 'resolve_target_date', return_value=dict(targetDate=TARGET, shouldRun=True)))
        self.stack.enter_context(patch.object(scan, 'load_universe', return_value=self.universe))
        self.stack.enter_context(patch.object(scan, 'fetch_stock', side_effect=fetch))
        self.stack.enter_context(patch.object(scan, 'Bridge', FakeBridge))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_full_scan_marks_unsupported_and_does_not_call_it_no_signal(self):
        result = scan.run(workers=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['ok'], 2)
        self.assertEqual(result['coverage']['unsupported'], 1)
        self.assertEqual(result['coverage']['failed'], 0)
        self.assertEqual(len(result['uncovered']), 1)
        self.assertIn('不代表全市场没有', scan.markdown_report(result))

    def test_sample_does_not_overwrite_full_latest(self):
        reports = scan.REPORTS
        reports.mkdir()
        (reports / 'latest.json').write_text('{"sentinel":true}', encoding='utf-8')
        result = scan.run(workers=1, limit=1)
        self.assertEqual(result['status'], 'sample')
        self.assertEqual(result['coverage']['requested'], 1)
        self.assertEqual(json.loads((reports / 'latest.json').read_text()), {'sentinel': True})

    def test_old_universe_policy_cannot_publish_new_scope_report(self):
        self.universe['universePolicy'] = 'old-policy-including-st-and-bj'
        with self.assertRaisesRegex(RuntimeError, '股票池范围版本不匹配'):
            scan.run(workers=1)
        self.assertEqual(self.fetch_calls, [])
        self.assertFalse((scan.REPORTS / 'latest.json').exists())

    def test_future_input_bars_are_removed_before_bridge(self):
        self.payload['bars'] = self.bars + [dict(time='2026-11-04', close=999)]
        scan.run(workers=1)
        self.assertTrue(self.bridge_calls)
        self.assertTrue(all(bar['time'] <= TARGET for _, bars, _ in self.bridge_calls for bar in bars))

    def test_valid_completed_checkpoint_skips_repeat_fetch(self):
        scan.run(workers=1)
        self.fetch_calls.clear()
        result = scan.run(workers=1)
        self.assertEqual(self.fetch_calls, [])
        self.assertEqual(result['coverage']['ok'], 2)

    def test_engine_change_during_run_prevents_report_publication(self):
        original = self.payload
        def changing_fetch(stock, force=False):
            (self.root / 'engine.js').write_text('CHANGED fixture engine', encoding='utf-8')
            return original, None
        with patch.object(scan, 'fetch_stock', side_effect=changing_fetch):
            with self.assertRaisesRegex(RuntimeError, '算法发生变化'):
                scan.run(workers=1)
        self.assertFalse((scan.REPORTS / 'latest.json').exists())

    def test_checkpoint_evidence_hash_date_and_pipeline_are_required(self):
        run_dir = self.root / 'checkpoint-fixture'
        inputs = run_dir / 'inputs'
        inputs.mkdir(parents=True)
        raw = json.dumps(self.payload).encode('utf-8')
        file = inputs / '600001.json.gz'
        with gzip.open(file, 'wb') as output:
            output.write(raw)
        row = {**self.stocks[0], 'status': 'ok', 'targetDate': TARGET, 'asOf': TARGET,
               'signals': [], 'engineSha256': self.engine_hash, 'pipelineSha256': 'PIPELINE-A', 'inputSha256': scan.sha(raw)}
        self.assertTrue(scan.valid_checkpoint(row, TARGET, self.engine_hash, 'PIPELINE-A', run_dir))
        for field, bad in [('status', 'error'), ('targetDate', '2026-11-02'), ('asOf', '2026-11-02'),
                           ('engineSha256', 'WRONG'), ('pipelineSha256', 'WRONG'), ('inputSha256', 'WRONG')]:
            self.assertFalse(scan.valid_checkpoint({**row, field: bad}, TARGET, self.engine_hash, 'PIPELINE-A', run_dir), field)
        file.unlink()
        self.assertFalse(scan.valid_checkpoint(row, TARGET, self.engine_hash, 'PIPELINE-A', run_dir))
        file.write_bytes(b'not gzip')
        self.assertFalse(scan.valid_checkpoint(row, TARGET, self.engine_hash, 'PIPELINE-A', run_dir))

    def test_presentation_sort_preserves_flagged_signals_but_places_them_after_clear(self):
        def record(code, turnover, invalid=False, conflict=False):
            return dict(code=code, name='模拟普通股票', exchange='SH', status='ok', st=False, volume=100,
                        barCount=200, turnoverProxyCNY=turnover,
                        signals=[dict(type='B1', invalidatedAtClose=invalid, concurrentSell=conflict)])
        records = [record('600001', 20), record('600002', 100), record('600003', 999, invalid=True),
                   record('600005', 9999, conflict=True), dict(code='600004', status='fetch_error', signals=[])]
        result = scan.group_candidates(records)
        self.assertEqual([r['code'] for r in result], ['600002', '600001', '600005', '600003'])
        self.assertEqual(result[2]['flags'], ['同日买卖信号冲突'])
        self.assertEqual(result[3]['flags'], ['收盘已达失效位'])

    def test_defensive_candidate_filter_rejects_st_names_and_beijing(self):
        base = dict(code='600001', name='模拟普通股票', exchange='SH', status='ok', st=False, volume=100,
                    barCount=200, turnoverProxyCNY=1000,
                    signals=[dict(type='B1', invalidatedAtClose=False, concurrentSell=False)])
        excluded = [
            {**base, 'code': '600002', 'st': True},
            {**base, 'code': '600003', 'name': '*ST模拟'},
            {**base, 'code': '600004', 'name': 'ST模拟'},
            {**base, 'code': '600005', 'exchange': 'BJ'},
            {**base, 'code': '920001'},
            {**base, 'code': '830001'},
            {**base, 'code': '430001'},
            {**base, 'code': '870001'},
            {**base, 'code': '688001'},
            {**base, 'code': '300001', 'exchange': 'SZ'},
        ]
        result = scan.group_candidates([base] + excluded)
        self.assertEqual([r['code'] for r in result], ['600001'])

    def test_completed_source_input_remains_valid_across_midnight(self):
        self.assertTrue(scan.completed_input_evidence(self.payload,TARGET,NOW))
        following_day=datetime(2026,11,5,0,1,tzinfo=NOW.tzinfo)
        self.assertTrue(scan.completed_input_evidence(self.payload,TARGET,following_day))
        self.assertEqual(self.payload['meta']['fetchedAt'],TARGET+'T16:00:00+08:00')

    def test_preclose_and_unconfirmed_same_day_inputs_are_rejected(self):
        for changes in [
            {'fetchedAt':TARGET+'T14:59:59+08:00'},
            {'sourceSessionConfirmed':False},
            {'sourceRegularMarketTime':datetime.fromisoformat(TARGET+'T14:59:59+08:00').timestamp()},
            {'readiness':'awaiting_source'},
            {'completenessPolicy':'exclude_exchange_today'},
            {'expectedThrough':'2026-11-02'},
            {'stale':True},
        ]:
            payload={**self.payload,'meta':{**self.payload['meta'],**changes}}
            self.assertFalse(scan.completed_input_evidence(payload,TARGET,NOW),changes)

    def test_resume_refreshes_security_name_without_rewriting_price_evidence(self):
        first=scan.run(workers=1)
        self.stocks[0]['name']='模拟新名称'
        self.fetch_calls.clear()
        second=scan.run(workers=1)
        self.assertEqual(self.fetch_calls,[])
        rows=[json.loads(line) for line in (Path(second['runDirectory'])/'results.jsonl').read_text(encoding='utf8').splitlines()]
        latest={r['code']:r for r in rows}
        self.assertEqual(latest['600001']['name'],'模拟新名称')
        self.assertEqual(latest['600001']['dataMeta']['fetchedAt'],TARGET+'T16:00:00+08:00')


if __name__ == '__main__':
    unittest.main(verbosity=2)
