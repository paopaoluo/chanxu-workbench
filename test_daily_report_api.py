import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

import server
import daily_report_api as api

def sha(raw): return hashlib.sha256(raw).hexdigest().upper()

class DailyReportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name); self.reports=self.root/'daily-reports';self.reports.mkdir()
        self.engine=b'fixture engine bytes';(self.root/'engine.js').write_bytes(self.engine)
        self.now=datetime.fromisoformat('2026-11-05T00:40:00+08:00')
        self.patches=[patch.object(api,'PROJECT_DIR',self.root),patch.object(api,'REPORTS_DIR',self.reports),patch.object(api,'now_cn',return_value=self.now)]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.temp.cleanup)
        self.make_report()

    def make_report(self, target='2026-11-04', empty=False):
        key=target+'-'+sha(self.engine)[:12]+'-'+'A'*8
        self.run=self.reports/key; (self.run/'inputs').mkdir(parents=True,exist_ok=True)
        sig={'id':'B1-3','type':'B1','confirmationDate':target,'pivotDate':'2026-11-03','rawIndex':4,'rawPivotIndex':3,'price':9.,'stop':9.}
        candidate={'code':'600000','name':'浦发银行','symbol':'600000.SS','exchange':'SH','board':'沪市主板','st':False,'close':11.,'volume':100.,'turnoverProxyCNY':1100.,'barCount':5,'signals':[sig],'flags':[]}
        self.report={'status':'partial','generatedAt':'2026-11-05T00:30:00+08:00','targetDate':target,'calendar':{'targetDate':target,'asOf':'2026-11-05','dateVerified':True,'latestObservedTradingDate':target},'engineSha256':sha(self.engine),'pipelineSha256':'A'*64,'ruleVersion':'chan-proxy-0.2-causal-events','universePolicy':'sh_sz_main_no_st_v2','historyRange':'2y','adjustment':'unadjusted','coverage':{'universeTotal':5,'inScope':3,'supported':3,'requested':3,'ok':2,'unsupported':0,'failed':1,'statuses':{'ok':2,'fetch_error':1}},'candidates':[] if empty else [candidate],'uncovered':[{'code':'600001','status':'fetch_error','error':'行情源连接失败'}],'runDirectory':str(self.run)}
        dates=['2026-10-29','2026-10-30','2026-11-02','2026-11-03',target]
        self.input={'bars':[{'time':d,'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.} for d in dates],'meta':{'symbol':'600000.SS','source':'Yahoo Finance chart API','range':'2y','interval':'1d','adjustment':'unadjusted OHLC; adjusted close is not applied','asOf':target,'fetchedAt':'2026-11-04T16:20:00+00:00'}}
        raw=json.dumps(self.input).encode();gzip.open(self.run/'inputs/600000.json.gz','wb').write(raw)
        self.row={**candidate,'status':'ok','asOf':target,'targetDate':target,'engineSha256':sha(self.engine),'pipelineSha256':'A'*64,'inputSha256':sha(raw),'dataMeta':copy.deepcopy(self.input['meta'])}
        self.save_row();self.save_report()
        (self.reports/'progress.json').write_text(json.dumps({'status':'finished','targetDate':target,'updatedAt':'2026-11-05T00:39:00+08:00','processed':3,'requested':3,'runDirectory':str(self.run)}),encoding='utf8')

    def save_row(self):
        (self.run/'results.jsonl').write_text(json.dumps(self.row,ensure_ascii=False)+'\n',encoding='utf8')

    def save_report(self):
        raw=json.dumps(self.report,ensure_ascii=False).encode()
        (self.run/'report.json').write_bytes(raw);(self.reports/'latest.json').write_bytes(raw)
        self.report_sha=sha(raw)

    def snapshot(self,**overrides):
        p={'report_key':self.run.name,'report_sha256':self.report_sha,'code':'600000'};p.update(overrides)
        return api.get_snapshot(**p)

    def test_current_partial_preserves_coverage_and_summary(self):
        x=api.get_daily_signals()
        self.assertEqual(x['reportState'],'current');self.assertEqual(x['report']['status'],'partial')
        self.assertEqual(x['report']['coverage'],self.report['coverage'])
        self.assertEqual(x['summary']['candidateCount'],1);self.assertEqual(x['summary']['signalCount'],1)
        self.assertEqual(x['summary']['typeCounts']['B1'],1);self.assertEqual(x['summary']['uncoveredReasons'][0]['count'],1)
        self.assertEqual(x['progress']['freshness'],'recent')

    def test_missing_report(self):
        (self.reports/'latest.json').unlink()
        self.assertEqual(api.get_daily_signals()['reportState'],'missing')

    def test_old_date_is_not_current(self):
        self.make_report('2026-11-03')
        self.assertEqual(api.get_daily_signals()['reportState'],'outdated')

    def test_empty_candidates_valid(self):
        self.make_report(empty=True)
        x=api.get_daily_signals();self.assertEqual(x['reportState'],'current');self.assertEqual(x['summary']['candidateCount'],0)

    def test_invalid_sample_or_scope_or_future_dates(self):
        for key,value in [('status','sample'),('universePolicy','old_scope'),('targetDate','2026-11-06')]:
            with self.subTest(key=key):
                before=copy.deepcopy(self.report);self.report[key]=value;self.save_report()
                self.assertEqual(api.get_daily_signals()['reportState'],'invalid')
                self.report=before;self.save_report()

    def test_calendar_failure_does_not_guess(self):
        with patch.object(api,'market_status',side_effect=ValueError('unknown year')):
            x=api.get_daily_signals()
        self.assertEqual(x['reportState'],'calendar_unavailable');self.assertIsNone(x['expectedTargetDate'])

    def test_non_trading_target_or_before_close_generated_report_invalid(self):
        self.make_report('2026-11-01')
        self.assertEqual(api.get_daily_signals()['reportState'],'invalid')
        self.make_report()
        self.report['generatedAt']='2026-11-04T14:30:00+08:00';self.report['calendar']['asOf']='2026-11-04';self.save_report()
        self.assertEqual(api.get_daily_signals()['reportState'],'invalid')

    def test_changed_latest_rejected(self):
        x=copy.deepcopy(self.report);x['ruleVersion']='altered'
        (self.reports/'latest.json').write_text(json.dumps(x),encoding='utf8')
        self.assertEqual(api.get_daily_signals()['reportState'],'invalid')

    def test_stale_progress_and_unknown_progress(self):
        (self.reports/'progress.json').write_text(json.dumps({'updatedAt':'2026-11-04T10:00:00+08:00','status':'running'}),encoding='utf8')
        self.assertEqual(api.get_daily_signals()['progress']['freshness'],'stale')
        (self.reports/'progress.json').unlink()
        self.assertEqual(api.get_daily_signals()['progress']['freshness'],'unknown')

    def test_snapshot_original_bars_and_confirmed_index(self):
        x=self.snapshot();self.assertTrue(x['engineCompatible']);self.assertEqual(x['bars'],self.input['bars'])
        self.assertEqual(x['meta'],self.input['meta']);self.assertEqual(x['report']['reportKey'],self.run.name)
        self.assertEqual(x['candidate']['signals'][0]['rawIndex'],4)
        self.assertEqual(x['report']['reportSha256'],self.report_sha)

    def test_snapshot_path_traversal_and_non_candidate_rejected(self):
        for params in [{'report_key':'../cache'},{'report_key':'..\\cache'},{'code':'../abc'},{'code':'600001'},{'report_sha256':'broken'}]:
            with self.subTest(params=params), self.assertRaises(api.ApiError): self.snapshot(**params)

    def test_snapshot_report_sha_changed_rejected(self):
        with self.assertRaises(api.ApiError): self.snapshot(report_sha256='B'*64)

    def test_snapshot_input_hash_changed_rejected(self):
        self.row['inputSha256']='B'*64;self.save_row()
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_engine_incompatible_rejected(self):
        (self.root/'engine.js').write_bytes(b'new version')
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_latest_checkpoint_not_older_success(self):
        with (self.run/'results.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps({**self.row,'status':'fetch_error'})+'\n')
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_future_bars_rejected(self):
        self.input['bars'][-1]['time']='2026-11-05';raw=json.dumps(self.input).encode()
        gzip.open(self.run/'inputs/600000.json.gz','wb').write(raw);self.row['inputSha256']=sha(raw);self.save_row()
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_wrong_confirmation_date_rejected(self):
        self.report['candidates'][0]['signals'][0]['confirmationDate']='2026-11-03';self.save_report()
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_mismatched_price_basis_rejected(self):
        self.input['meta']['adjustment']='adjusted';raw=json.dumps(self.input).encode()
        gzip.open(self.run/'inputs/600000.json.gz','wb').write(raw);self.row['inputSha256']=sha(raw);self.save_row()
        with self.assertRaises(api.ApiError):self.snapshot()

    def test_snapshot_metadata_and_displayed_quote_match_actual_input(self):
        for mode in ('metadata','source','close','volume'):
            with self.subTest(mode=mode):
                self.make_report()
                if mode=='metadata':self.row['dataMeta']['fetchedAt']='2026-11-04T16:29:00+00:00'
                elif mode=='source':
                    self.input['meta']['source']='different source';self.row['dataMeta']['source']='different source'
                    raw=json.dumps(self.input).encode();gzip.open(self.run/'inputs/600000.json.gz','wb').write(raw);self.row['inputSha256']=sha(raw)
                else:
                    self.report['candidates'][0][mode]+=1
                    self.row[mode]+=1
                self.save_row();self.save_report()
                with self.assertRaises(api.ApiError):self.snapshot()

    def test_http_both_routes_and_clear_error(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{http.server_port}'
        try:
            with urlopen(base+'/api/daily-signals') as result:listing=json.load(result)
            self.assertEqual(listing['schema'],'chanxu-daily-signals-v1')
            params=urlencode({'report':self.run.name,'sha':self.report_sha,'code':'600000'})
            with urlopen(base+'/api/daily-signals/snapshot?'+params) as result:snapshot=json.load(result)
            self.assertEqual(snapshot['candidate']['code'],'600000')
            with self.assertRaises(HTTPError) as caught:urlopen(base+'/api/daily-signals/snapshot?report=../cache&sha=x&code=600000')
            self.assertEqual(caught.exception.code,400);self.assertNotIn('Traceback',caught.exception.read().decode())
        finally:http.shutdown();http.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
