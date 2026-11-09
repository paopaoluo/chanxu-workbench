"""Daily confirmed B-point scan; exact web engine, same 2y unadjusted Yahoo bars.

Run: python daily_scan.py [--workers 8] [--limit 20] [--force]
No orders. Missing stocks/date verification failures are explicit, not no-signal.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import server
from screener_data import load_universe, resolve_target_date, now_cn
from market_clock import CN, market_status

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / 'daily-reports'

def sha(data):
    return hashlib.sha256(data).hexdigest().upper()

def pipeline_sha():
    files = ['engine.js','daily_scan_engine.js','screener_data.py','server.py','daily_scan.py','market_clock.py']
    return sha('\n'.join(f'{name}:{sha((ROOT/name).read_bytes())}' for name in files).encode('utf-8'))

def completed_input_evidence(payload, target, latest=None):
    """Accept frozen target-session inputs across midnight, never a preclose bar."""
    try:
        meta, bars = payload['meta'], payload['bars']
        fetched = datetime.fromisoformat(meta['fetchedAt'])
        if fetched.tzinfo is None:
            return False
        fetched = fetched.astimezone(CN)
        latest = latest or now_cn()
        close_at = datetime.fromisoformat(target+'T15:00:00+08:00')
        if not (close_at <= fetched <= latest):
            return False
        if (meta.get('stale') or meta.get('readiness') != 'ready'
                or meta.get('completenessPolicy') != 'cn_close_v1'
                or meta.get('expectedThrough') != target):
            return False
        if fetched.date().isoformat() == target:
            source_time = meta.get('sourceRegularMarketTime')
            if (not meta.get('sourceSessionConfirmed') or not isinstance(source_time, (int,float))
                    or source_time < close_at.timestamp()):
                return False
        dates = [b['time'] for b in bars]
        return bool(dates and dates[-1] == target and dates == sorted(set(dates))
                    and all(d <= target for d in dates))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def valid_checkpoint(row, target, engine_hash, pipeline_hash, run_dir):
    if not (row.get('status') == 'ok' and row.get('engineSha256') == engine_hash
            and row.get('pipelineSha256') == pipeline_hash and row.get('targetDate') == target
            and row.get('asOf') == target):
        return False
    code = row.get('code','')
    if not (isinstance(code,str) and len(code)==6 and code.isascii() and code.isdigit()):
        return False
    try:
        with gzip.open(run_dir/'inputs'/(code+'.json.gz'),'rb') as f:
            raw = f.read()
        return sha(raw) == row.get('inputSha256') and completed_input_evidence(json.loads(raw), target)
    except (OSError, EOFError, ValueError):
        return False

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

@contextmanager
def scan_lock():
    import msvcrt
    REPORTS.mkdir(exist_ok=True)
    with (REPORTS / 'scan.lock').open('a+b') as f:
        f.seek(0)
        if not f.read(1):
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('已有扫描在运行；请读取 daily-reports/progress.json，勿重复启动')
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

def fetch_stock(stock, force=False):
    last = None
    for attempt in range(3):
        try:
            data = server.get_history(stock['symbol'], '2y', refresh=force or attempt > 0)
            if data.get('meta', {}).get('stale') or data.get('meta', {}).get('readiness') != 'ready':
                raise RuntimeError('行情未就绪：等待数据源发布完整收盘日线；过期缓存和盘中K线不用于筛选')
            return data, None
        except Exception as exc:
            cause = getattr(exc,'__cause__',None)
            last = str(exc) + (f' [{type(cause).__name__}: {cause}]' if cause else '')
            if '404' in last:
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, last

class BridgeError(RuntimeError):
    """One input could not be processed; this is not an algorithm-version error."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Bridge:
    """Serial NDJSON requests with a deadline covering writes AND response reads.

    A failed process is never reused. The daemon I/O thread owns only its captured
    process and result queue, so a late result cannot leak into the next request.
    No Windows select(pipe), and no unbounded thread/process joins on shutdown.
    """
    def __init__(self, timeout=30.0, shutdown_timeout=2.0, command=None):
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
                   for x in (timeout, shutdown_timeout)):
            raise ValueError('Bridge timeout values must be finite and positive')
        self.timeout = float(timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self.command = command
        self.proc = None
        self.worker = None
        self.cleanup_failed = False

    def _start(self):
        if self.cleanup_failed:
            raise RuntimeError('旧Node进程或I/O线程未能回收，停止扫描以避免资源泄漏')
        if self.command is None:
            node = shutil.which('node')
            if not node:
                raise RuntimeError('未找到 Node.js；无法调用网页原算法')
            command = [node, str(ROOT/'daily_scan_engine.js')]
        else:
            command = self.command
        self.proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=sys.stderr,
                                     text=True, encoding='utf-8', bufsize=1,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def __enter__(self):
        self._start()
        return self

    def _discard(self):
        proc, worker = self.proc, self.worker
        self.proc = self.worker = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=self.shutdown_timeout)
        except (OSError, subprocess.TimeoutExpired):
            self.cleanup_failed = True
        if worker is not None:
            worker.join(timeout=self.shutdown_timeout)
            if worker.is_alive():
                # Do not close a TextIOWrapper while another thread owns its lock.
                # Never start another worker after an unreapable I/O operation.
                self.cleanup_failed = True
                return
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _validate_response(value, stock, target):
        if (not isinstance(value, dict)
                or value.get('status') not in ('ok', 'error', 'engine_error', 'stale', 'insufficient')
                or not isinstance(value.get('signals'), list)
                or value.get('code') != stock['code']
                or value.get('symbol') != stock['symbol']
                or value.get('targetDate') != target
                or not isinstance(value.get('engineSha256'), str)
                or len(value['engineSha256']) != 64
                or any(c not in '0123456789abcdefABCDEF' for c in value['engineSha256'])):
            raise BridgeError('protocol', 'Node响应协议或证券/目标日身份不匹配')
        # The orchestrator still checks this hash against the frozen run version.
        return value

    def analyze(self, stock, bars, target):
        if self.proc is None:
            self._start()
        proc = self.proc
        replies = queue.Queue(maxsize=1)

        def exchange():
            try:
                request = json.dumps({**stock, 'bars': bars, 'targetDate': target},
                                     ensure_ascii=False, allow_nan=False) + '\n'
                proc.stdin.write(request)
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise BridgeError('eof', 'Node分析进程意外退出或输出EOF')
                if not line.endswith('\n'):
                    raise BridgeError('protocol', 'Node响应缺少NDJSON换行终止符')
                value = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError('nonfinite JSON value: ' + value)))
                value = self._validate_response(value, stock, target)
                replies.put((time.monotonic(), True, value))
            except BridgeError as exc:
                replies.put((time.monotonic(), False, exc))
            except Exception as exc:
                code = 'pipe' if isinstance(exc, (OSError, BrokenPipeError)) else 'protocol'
                replies.put((time.monotonic(), False,
                             BridgeError(code, f'Node通信失败：{type(exc).__name__}: {exc}')))

        self.worker = threading.Thread(target=exchange, name='chanxu-engine-request', daemon=True)
        deadline = time.monotonic() + self.timeout
        self.worker.start()
        try:
            finished, success, value = replies.get(timeout=max(0.0, deadline-time.monotonic()))
            if finished > deadline:
                raise queue.Empty
        except queue.Empty:
            self._discard()
            raise BridgeError('timeout', f'Node分析超时（{self.timeout:g}秒，含请求写入与响应读取）') from None
        if not success:
            self._discard()
            raise value
        self.worker.join(timeout=self.shutdown_timeout)
        self.worker = None
        if value['status'] == 'engine_error':
            self._discard()
        return value

    def __exit__(self, *args):
        self._discard()

def group_candidates(records):
    out = []
    for r in records:
        code = r.get('code','')
        if not (isinstance(code,str) and len(code)==6 and code.isascii() and code.isdigit()
                and code.startswith(('600','601','603','605','000','001','002','003'))):
            continue
        if r.get('exchange') == 'BJ' or r.get('st') or 'ST' in r.get('name','').upper():
            continue
        if r.get('status') != 'ok' or not r.get('signals'):
            continue
        flags = []
        if r.get('st'): flags.append('ST')
        if r.get('volume', 0) <= 0: flags.append('成交量为零')
        if any(s.get('invalidatedAtClose') for s in r['signals']): flags.append('收盘已达失效位')
        if any(s.get('concurrentSell') for s in r['signals']): flags.append('同日买卖信号冲突')
        if r.get('barCount', 0) < 120: flags.append('历史不足120根')
        out.append({k:r.get(k) for k in ('code','name','symbol','exchange','board','st','close','volume','turnoverProxyCNY','barCount','signals')} | {'flags':flags})
    # Presentation ranking only: unflagged first, then close*share-volume.
    # No claim this estimates expected return or probability of success.
    out.sort(key=lambda r:(bool(r['flags']), -(r.get('turnoverProxyCNY') or 0), r['code']))
    return out

def markdown_report(report, all_candidates=False):
    c = report['coverage']
    lines = [f"# {report['targetDate']} 新确认B点观察清单", '',
      f"生成：{report['generatedAt']}；最新已收盘交易日的信号确认日严格等于 {report['targetDate']}（不是转折极值日）。",
      f"状态：**{report['status']}**；范围：仅沪深主板普通A股，排除科创板、创业板、北交所及ST（含*ST）。",
      f"股票池 {c['universeTotal']}；范围内 {c['inScope']}；完成分析 {c['ok']}；接口不支持 {c['unsupported']}；错误/缺失/历史不足 {c['failed']}；发现 {len(report['candidates'])} 只新B点。",
      '不支持或缺数据的股票不得视为无B点；北交所与ST为用户明确排除，不计入本次应覆盖范围。',
      '', '## 全部符合条件的股票' if all_candidates else '## 优先复核（最多10只）', '',
      '排序仅按无标记优先、目标日收盘价×成交股数降序（流动性近似值），不是胜率或收益预测。',
      '|代码|名称|B类型|确认日收盘|结构失效参考|距失效位|标记|',
      '|---|---|---|---:|---|---|---|']
    for r in (report['candidates'] if all_candidates else report['candidates'][:10]):
        sigs=r['signals']
        labels='/'.join(s['type'] for s in sigs)
        stops='/'.join(f"{s['type']}:{s['stop']:.3f}" if s.get('stop') is not None else f"{s['type']}:无" for s in sigs)
        distance='/'.join(f"{s['distanceToStopPct']:.2f}%" if s.get('distanceToStopPct') is not None else '—' for s in sigs)
        lines.append(f"|{r['code']}|{r['name']}|{labels}|{r['close']:.3f}|{stops}|{distance}|{'、'.join(r['flags']) or '—'}|")
    if not report['candidates']:
        lines.append('本次已成功分析的范围内没有符合条件的新B点；有未覆盖证券时不代表全市场没有。')
    lines += ['', '## 计算与留存',
      '- 直接调用当前网页 engine.js，2年未复权日线；B1/B2/B3为现有实验代理规则，未引入其它选股指标。',
      '- 所有输入裁剪到目标日；B点转折日、确认日、中枢证据及原因保存在 report.json。',
      '- report.json 保存全部候选及未覆盖明细，inputs/*.json.gz 保存实际输入；没有自动委托。',
      f"- engine SHA256：`{report['engineSha256']}`；版本：`{report['ruleVersion']}`。",
      '- 收盘失效/同日冲突等项目保留并标记；北交所和ST已按用户要求排除。', '']
    return '\n'.join(lines)

def run(workers=8, limit=None, force=False):
    with scan_lock():
        started=now_cn().isoformat()
        calendar=resolve_target_date()
        if not calendar['shouldRun']:
            result={'status':'market_closed','generatedAt':started,'calendar':calendar}
            write_json(REPORTS/'latest.json',result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            return result
        target=calendar['targetDate']
        universe=load_universe()
        engine_hash=sha((ROOT/'engine.js').read_bytes())
        pipeline_hash=pipeline_sha()
        run_id=f"{target}-{engine_hash[:12]}-{pipeline_hash[:8]}"+('-sample'+str(limit) if limit else '')
        run_dir=REPORTS/run_id
        run_dir.mkdir(parents=True,exist_ok=True)
        write_json(run_dir/'universe.json',universe)
        if universe.get('universePolicy') != 'sh_sz_main_no_st_v2':
            raise RuntimeError('股票池范围版本不匹配，拒绝使用含北交所或ST的旧名单')
        supported=[s for s in universe['stocks'] if s['symbol']]
        if limit: supported=supported[:limit]
        stock_by_code={s['code']:s for s in supported}
        eligible_codes=set(stock_by_code)
        checkpoint=run_dir/'results.jsonl'
        results={}
        if checkpoint.exists() and not force:
            for line in checkpoint.read_text(encoding='utf-8').splitlines():
                try:
                    row=json.loads(line)
                    if valid_checkpoint(row,target,engine_hash,pipeline_hash,run_dir) and row['code'] in eligible_codes:
                        stock = stock_by_code[row['code']]
                        # Refresh universe identity only; the frozen price/source evidence stays unchanged.
                        results[row['code']]={**row, **stock}
                except (ValueError,KeyError): pass
        todo=[s for s in supported if s['code'] not in results]
        count=len(results)
        def progress(status):
            value={'status':status,'targetDate':target,'startedAt':started,'updatedAt':now_cn().isoformat(),'processed':len(results),'requested':len(supported),'resumeSkipped':count,'runDirectory':str(run_dir)}
            write_json(REPORTS/'progress.json',value)
        progress('running')
        print(f"SCAN target={target} stocks={len(supported)} resume={count} unsupported={universe['unsupportedHistoryCount']}",flush=True)
        with Bridge() as bridge, ThreadPoolExecutor(max_workers=workers) as pool, checkpoint.open('a',encoding='utf-8') as log:
            # Keep append-only history while the final rows reflect today's universe names/classification.
            for row in results.values():
                log.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
            log.flush()
            futures={pool.submit(fetch_stock,s,force):s for s in todo}
            for future in as_completed(futures):
                stock=futures[future]
                payload,error=future.result()
                if error:
                    row={**stock,'targetDate':target,'status':'fetch_error','error':error,'signals':[],'engineSha256':engine_hash}
                else:
                    used=[b for b in payload['bars'] if b['time']<=target]
                    raw=json.dumps({'meta':payload['meta'],'bars':used},ensure_ascii=False,allow_nan=False).encode('utf-8')
                    if not completed_input_evidence({'meta':payload['meta'],'bars':used}, target):
                        raise RuntimeError(f"{stock['code']}收盘证据不符或扫描跨越了收盘边界，请按最新交易日重新运行")
                    inputs=run_dir/'inputs'; inputs.mkdir(exist_ok=True)
                    with gzip.open(inputs/(stock['code']+'.json.gz'),'wb') as inp: inp.write(raw)
                    try:
                        analysis = bridge.analyze(stock, used, target)
                    except BridgeError as exc:
                        analysis = {'status': 'engine_error', 'error': str(exc),
                                    'errorCode': exc.code, 'signals': [], 'targetDate': target,
                                    'asOf': used[-1]['time'] if used else None,
                                    'barCount': len(used), 'engineSha256': engine_hash}
                    row={**stock,**analysis,'inputSha256':sha(raw),'dataMeta':payload['meta']}
                    if row['engineSha256']!=engine_hash:
                        raise RuntimeError('算法文件已变化，停止混合版本扫描')
                    if row['status'] == 'engine_error':
                        row.setdefault('error', 'Node返回engine_error，未提供错误详情')
                        row.setdefault('errorCode', 'engine_response')
                        print(json.dumps({'event': 'engine_error', 'code': stock['code'],
                                          'targetDate': target, 'errorCode': row['errorCode'],
                                          'error': row['error']}, ensure_ascii=False),
                              file=sys.stderr, flush=True)
                    last=used[-1] if used else {}
                    row['volume']=last.get('volume',0)
                    row['turnoverProxyCNY']=last.get('close',0)*last.get('volume',0)
                row['pipelineSha256']=pipeline_hash
                results[stock['code']]=row
                log.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n'); log.flush()
                if len(results)%50==0 or len(results)==len(supported):
                    progress('running')
                    print(f"PROGRESS {len(results)}/{len(supported)} B_stocks={sum(bool(r.get('signals')) for r in results.values())}",flush=True)
        if sha((ROOT/'engine.js').read_bytes())!=engine_hash or pipeline_sha()!=pipeline_hash:
            raise RuntimeError('扫描期间算法发生变化，拒绝发布混合版本结果')
        if calendar.get('targetPolicy') == 'cn_close_v1' and market_status(now_cn())['targetDate'] != target:
            raise RuntimeError('扫描跨越收盘边界，目标交易日已更新；保留检查点，请重新运行最新目标日')
        statuses=Counter(r['status'] for r in results.values())
        unsupported=[{**s,'status':'unsupported','error':s['coverageNote']} for s in universe['stocks'] if not s['symbol']]
        failures=[r for r in results.values() if r['status']!='ok']
        candidates=group_candidates(results.values())
        report={'status':'sample' if limit else 'partial' if unsupported or failures else 'complete',
          'generatedAt':now_cn().isoformat(),'startedAt':started,'targetDate':target,'calendar':calendar,
          'engineSha256':engine_hash,'pipelineSha256':pipeline_hash,'ruleVersion':next((r.get('ruleVersion') for r in results.values() if r.get('ruleVersion')),None),
          'historyRange':'2y','adjustment':'unadjusted','universeSource':universe['source'],'universePolicy':universe['universePolicy'],
          'coverage':{'universeTotal':universe['sourceTotal'],'inScope':universe['total'],'supported':universe['eligibleHistoryCount'],
                      'requested':len(supported),'ok':statuses['ok'],'unsupported':len(unsupported),'failed':len(failures),'statuses':dict(statuses)},
          'candidates':candidates,'uncovered':unsupported+failures,'runDirectory':str(run_dir)}
        write_json(run_dir/'report.json',report)
        (run_dir/'report.md').write_text(markdown_report(report),encoding='utf-8')
        (run_dir/'all-candidates.md').write_text(markdown_report(report,all_candidates=True),encoding='utf-8')
        if not limit: write_json(REPORTS/'latest.json',report)
        progress('finished')
        print(json.dumps({'status':report['status'],'targetDate':target,'coverage':report['coverage'],'candidateCount':len(candidates),'report':str(run_dir/'report.json')},ensure_ascii=False),flush=True)
        return report

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--workers',type=int,default=8,choices=range(1,9))
    parser.add_argument('--limit',type=int)
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    try:
        if args.limit is not None and args.limit<1: raise ValueError('limit must be positive')
        run(args.workers,args.limit,args.force)
    except Exception as exc:
        message={'status':'failed','at':now_cn().isoformat(),'error':str(exc)}
        # Never overwrite a running scan's progress or publish yesterday's report as today.
        write_json(REPORTS/('failure-'+str(os.getpid())+'.json'),message)
        print(json.dumps(message,ensure_ascii=False),file=sys.stderr,flush=True)
        sys.exit(1)
