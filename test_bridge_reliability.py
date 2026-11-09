"""Offline, bounded Node-process tests; no live scanner or market requests."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import daily_scan as scan

NODE = shutil.which('node')
TARGET = '2026-11-05'
STOCK = {'code': '600000', 'name': '离线测试', 'symbol': '600000.SS'}
ECHO = r'''const r=require('node:readline').createInterface({input:process.stdin});
r.on('line',line=>{const q=JSON.parse(line);process.stdout.write(JSON.stringify({
 code:q.code,name:q.name,symbol:q.symbol,targetDate:q.targetDate,status:'ok',asOf:q.targetDate,
 barCount:q.bars.length,signals:[],engineSha256:'A'.repeat(64),ruleVersion:'TEST ONLY'})+'\n');});'''


@unittest.skipUnless(NODE, 'Node runtime required')
class BridgeProcessTests(unittest.TestCase):
    def bridge(self, script, timeout=0.4):
        return scan.Bridge(timeout=timeout, shutdown_timeout=1.0, command=[NODE, '-e', script])

    def test_normal_response_identity_and_payload_are_unchanged(self):
        with self.bridge(ECHO) as bridge:
            result = bridge.analyze(STOCK, [], TARGET)
            self.assertEqual(result, dict(**STOCK, targetDate=TARGET, status='ok', asOf=TARGET,
                                         barCount=0, signals=[], engineSha256='A'*64, ruleVersion='TEST ONLY'))
            process = bridge.proc
        self.assertIsNotNone(process.poll())

    def test_eof_is_bounded_and_next_stock_uses_new_process(self):
        with self.bridge('process.stdin.once("data",()=>process.exit(7));') as bridge:
            old = bridge.proc
            with self.assertRaisesRegex(scan.BridgeError, '退出|EOF'):
                bridge.analyze(STOCK, [], TARGET)
            self.assertIsNotNone(old.poll())
            bridge.command = [NODE, '-e', ECHO]
            result = bridge.analyze({**STOCK, 'code': '600001', 'symbol': '600001.SS'}, [], TARGET)
            self.assertEqual(result['code'], '600001')
            self.assertNotEqual(old.pid, bridge.proc.pid)

    def test_no_output_timeout_is_bounded_and_recovers(self):
        with self.bridge('process.stdin.resume();setInterval(()=>{},1000);') as bridge:
            old = bridge.proc
            start = time.monotonic()
            with self.assertRaisesRegex(scan.BridgeError, '超时'):
                bridge.analyze(STOCK, [], TARGET)
            self.assertLess(time.monotonic()-start, 3)
            self.assertIsNotNone(old.poll())
            bridge.command = [NODE, '-e', ECHO]
            self.assertEqual(bridge.analyze(STOCK, [], TARGET)['status'], 'ok')

    def test_deadline_includes_large_stdin_write(self):
        with self.bridge('setInterval(()=>{},1000);') as bridge:
            old = bridge.proc
            start = time.monotonic()
            with self.assertRaisesRegex(scan.BridgeError, '超时'):
                bridge.analyze(STOCK, [{'time': TARGET, 'padding': 'x'*1000}]*16000, TARGET)
            self.assertLess(time.monotonic()-start, 3)
            self.assertIsNotNone(old.poll())

    def test_partial_line_without_newline_times_out(self):
        with self.bridge('process.stdin.once("data",()=>process.stdout.write("{\\\"status\\\":"));setInterval(()=>{},1000);') as bridge:
            with self.assertRaisesRegex(scan.BridgeError, '超时'):
                bridge.analyze(STOCK, [], TARGET)

    def test_bad_json_schema_and_wrong_identity_recycle_process(self):
        for output in ['not-json', '[]', '{"status":"ok"}', json.dumps({**STOCK, 'code':'600999',
                'targetDate':TARGET,'status':'ok','signals':[],'engineSha256':'A'*64})]:
            with self.subTest(output=output), self.bridge('process.stdin.once("data",()=>process.stdout.write('+json.dumps(output+'\n')+'));') as bridge:
                old=bridge.proc
                with self.assertRaises(scan.BridgeError): bridge.analyze(STOCK, [], TARGET)
                self.assertIsNotNone(old.poll())

    def test_explicit_engine_error_response_is_preserved_and_recycles(self):
        payload={**STOCK,'targetDate':TARGET,'status':'engine_error','signals':[],
                 'engineSha256':'A'*64,'error':'intentional test failure'}
        script='process.stdin.once("data",()=>process.stdout.write('+json.dumps(json.dumps(payload)+'\n')+'));setInterval(()=>{},1000);'
        with self.bridge(script) as bridge:
            old=bridge.proc
            self.assertEqual(bridge.analyze(STOCK,[],TARGET),payload)
            self.assertIsNotNone(old.poll())

    def test_actual_node_engine_matches_one_shot_frozen_input(self):
        fixture=Path(scan.ROOT)/'verification/watch-monitor-20261105/baseline-002156.SZ.json'
        if not fixture.exists(): self.skipTest('historical public fixture not present')
        payload=json.loads(fixture.read_text(encoding='utf-8')); bars=payload['bars'][-60:]
        target=bars[-1]['time']; request={**STOCK,'bars':bars,'targetDate':target}
        direct=subprocess.run([NODE,str(Path(scan.ROOT)/'daily_scan_engine.js')],input=json.dumps(request)+'\n',
                              text=True,encoding='utf-8',capture_output=True,timeout=10,check=True)
        with scan.Bridge(timeout=5) as bridge:
            actual=bridge.analyze(STOCK,bars,target)
        self.assertEqual(actual,json.loads(direct.stdout))

    def test_timeout_configuration_rejects_nonpositive_or_infinite(self):
        for timeout in [0,-1,float('inf'),float('nan')]:
            with self.subTest(timeout=timeout),self.assertRaises(ValueError): scan.Bridge(timeout=timeout)

    def test_deadline_starts_before_thread_start_and_slow_serialization(self):
        real_dumps = scan.json.dumps
        real_start = scan.threading.Thread.start
        def slow_dumps(*args, **kwargs):
            time.sleep(0.4)
            return real_dumps(*args, **kwargs)
        def delayed_start(thread):
            real_start(thread)
            time.sleep(0.25)
        with self.bridge(ECHO, timeout=0.3) as bridge:
            with patch.object(scan.json, 'dumps', side_effect=slow_dumps), \
                    patch.object(scan.threading.Thread, 'start', new=delayed_start):
                with self.assertRaisesRegex(scan.BridgeError, '超时'):
                    bridge.analyze(STOCK, [], TARGET)

    def test_actual_node_controlled_input_error_keeps_worker_for_next_request(self):
        with scan.Bridge(timeout=5) as bridge:
            proc=bridge.proc
            result=bridge.analyze(STOCK,[{'time':'not-a-date'}],TARGET)
            self.assertEqual(result['status'],'error')
            self.assertTrue(result['error'])
            self.assertIs(bridge.proc,proc)
            self.assertEqual(bridge.analyze(STOCK,[],TARGET)['status'],'insufficient')


class ScanEngineFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        for f in ['engine.js','daily_scan_engine.js','screener_data.py','server.py','daily_scan.py','market_clock.py']:
            (self.root/f).write_text('isolated-'+f,encoding='utf-8')
        self.sha=scan.sha((self.root/'engine.js').read_bytes())
        self.stocks=[{**STOCK,'code':f'60000{i}','symbol':f'60000{i}.SS','exchange':'SH','board':'沪市主板','st':False} for i in range(3)]
        self.universe={'stocks':self.stocks,'total':3,'sourceTotal':3,'source':'TEST ONLY',
                       'eligibleHistoryCount':3,'unsupportedHistoryCount':0,'universePolicy':'sh_sz_main_no_st_v2'}
        self.payload={'bars':[{'time':TARGET,'open':1,'high':2,'low':1,'close':2,'volume':100}],
                      'meta':{'stale':False,'readiness':'ready','completenessPolicy':'cn_close_v1',
                              'expectedThrough':TARGET,'fetchedAt':TARGET+'T16:00:00+08:00',
                              'sourceSessionConfirmed':True,
                              'sourceRegularMarketTime':datetime.fromisoformat(TARGET+'T15:00:00+08:00').timestamp()}}
        self.stack=ExitStack()
        self.stack.enter_context(patch.object(scan,'ROOT',self.root))
        self.stack.enter_context(patch.object(scan,'REPORTS',self.root/'reports'))
        self.stack.enter_context(patch.object(scan,'now_cn',return_value=datetime.fromisoformat(TARGET+'T23:00:00+08:00')))
        self.stack.enter_context(patch.object(scan,'resolve_target_date',return_value={'targetDate':TARGET,'shouldRun':True}))
        self.stack.enter_context(patch.object(scan,'load_universe',return_value=self.universe))
        self.stack.enter_context(patch.object(scan,'fetch_stock',return_value=(self.payload,None)))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.stack.close();self.temp.cleanup()

    def fake(self, failure):
        owner=self
        class Fake:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def analyze(self,stock,bars,target):
                if stock['code']=='600001':
                    if isinstance(failure,Exception): raise failure
                    return {**stock,'status':'engine_error','error':'explicit test error','signals':[],
                            'targetDate':target,'engineSha256':failure or owner.sha}
                return {**stock,'status':'ok','asOf':target,'targetDate':target,'barCount':len(bars),
                        'close':2,'signals':[],'engineSha256':owner.sha,'ruleVersion':'TEST ONLY'}
        return Fake

    def test_bridge_fault_keeps_successes_and_records_engine_error_checkpoint(self):
        with patch.object(scan,'Bridge',self.fake(scan.BridgeError('timeout','controlled timeout'))):
            report=scan.run(workers=1)
        self.assertEqual(report['status'],'partial');self.assertEqual(report['coverage']['ok'],2)
        self.assertEqual(report['coverage']['statuses']['engine_error'],1)
        self.assertEqual(report['coverage']['failed'],1)
        failed=report['uncovered'][0];self.assertEqual(failed['code'],'600001')
        self.assertEqual(failed['errorCode'],'timeout');self.assertTrue(failed['inputSha256'])
        self.assertEqual(failed['dataMeta']['fetchedAt'],TARGET+'T16:00:00+08:00')
        checkpoint=[json.loads(x) for x in (Path(report['runDirectory'])/'results.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(checkpoint),3)
        self.assertEqual(json.loads((scan.REPORTS/'progress.json').read_text())['status'],'finished')

    def test_explicit_engine_error_remains_failed_coverage(self):
        with patch.object(scan,'Bridge',self.fake(None)): report=scan.run(workers=1)
        self.assertEqual(report['coverage']['ok'],2);self.assertEqual(report['uncovered'][0]['status'],'engine_error')

    def test_version_mismatch_is_not_downgraded_to_stock_error(self):
        with patch.object(scan,'Bridge',self.fake('F'*64)),self.assertRaisesRegex(RuntimeError,'算法文件已变化'):
            scan.run(workers=1)
        self.assertFalse((scan.REPORTS/'latest.json').exists())


if __name__=='__main__': unittest.main()
