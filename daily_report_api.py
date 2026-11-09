"""Read-only dated-report and immutable-input APIs. No network and no scans."""
from collections import Counter
from datetime import date, datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import re

from market_clock import market_status, now_cn, is_trading_day, CN

PROJECT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_DIR/'daily-reports'
POLICY = 'sh_sz_main_no_st_v2'
REPORT_KEY = re.compile(r'\d{4}-\d{2}-\d{2}-[A-Fa-f0-9]{12}-[A-Fa-f0-9]{8}')
HASH = re.compile(r'[A-Fa-f0-9]{64}')
REPORT_FIELDS = ('targetDate','generatedAt','status','coverage','engineSha256','pipelineSha256',
                 'ruleVersion','universePolicy','historyRange','adjustment')


class ApiError(Exception):
    def __init__(self, message, status=409, code='invalid_report'):
        super().__init__(message)
        self.status, self.code = status, code


def _require(condition, message, status=409, code='invalid_report'):
    if not condition:
        raise ApiError(message, status, code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def _safe_file(parent, name):
    parent = Path(parent).resolve()
    path = (parent/name).resolve()
    _require(path.is_relative_to(parent) and path.is_relative_to(REPORTS_DIR.resolve()),
             '文件路径超出报告目录', 400, 'invalid_path')
    return path


def _read(path, limit=16*1024*1024):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(limit+1)
    except FileNotFoundError:
        raise ApiError('指定报告或快照文件不存在', 404, 'not_found')
    _require(len(raw)<=limit, '报告文件超过允许大小')
    return raw


def _json(raw):
    try:
        def invalid_constant(value):
            raise ValueError('non-finite JSON')
        return json.loads(raw.decode('utf-8-sig'), parse_constant=invalid_constant)
    except (UnicodeError, ValueError, TypeError):
        raise ApiError('报告或快照不是有效JSON')


def _day(value):
    _require(isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value), '报告日期格式无效')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError('报告日期无效')


def _aware(value):
    try:
        result=datetime.fromisoformat(value)
        _require(result.tzinfo is not None,'报告时间缺少时区')
        return result
    except (ValueError,TypeError):
        raise ApiError('报告时间格式无效')


def _finite(value, positive=False):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and (not positive or value>0)


def _validate_report(report, run, expected, now):
    _require(isinstance(report,dict),'报告格式无效')
    _require(report.get('status') in ('complete','partial'),'仅接受正式完整或部分覆盖报告，不接受样本')
    _require(report.get('universePolicy')==POLICY,'报告股票范围版本不匹配')
    target=report.get('targetDate');target_day=_day(target)
    _require(target_day<=_day(expected),'报告日期晚于最近已收盘交易日')
    try:
        _require(is_trading_day(target_day),'报告目标日不是交易日')
    except ValueError:
        raise ApiError('报告目标日无法通过交易日历核验')
    generated=_aware(report.get('generatedAt')).astimezone(CN)
    _require(generated>=datetime.fromisoformat(target+'T15:00:00+08:00') and (generated-now).total_seconds()<=30,'报告生成时间早于收盘或晚于当前时间')
    calendar=report.get('calendar') or {}
    _require(calendar.get('dateVerified') is True and calendar.get('targetDate')==target
             and calendar.get('latestObservedTradingDate')==target,'报告交易日期核验证据不一致')
    _require(target_day<=_day(calendar.get('asOf'))<=generated.date(),'报告交易日历核验时间异常')
    for name in ('engineSha256','pipelineSha256'):
        _require(isinstance(report.get(name),str) and HASH.fullmatch(report[name]),'报告版本哈希无效')
    expected_key=target+'-'+report['engineSha256'][:12]+'-'+report['pipelineSha256'][:8]
    _require(run.name.upper()==expected_key.upper(),'报告目录与日期、版本不一致')
    claimed=Path(report.get('runDirectory',''))
    _require(claimed.is_absolute() and claimed.resolve()==run.resolve(),'报告目录指向不一致')
    _require(report.get('historyRange')=='2y' and report.get('adjustment')=='unadjusted','报告行情窗口或复权口径不一致')
    cov=report.get('coverage')
    _require(isinstance(cov,dict),'报告缺少扫描覆盖信息')
    for key in ('universeTotal','inScope','supported','requested','ok','unsupported','failed'):
        _require(type(cov.get(key)) is int and cov[key]>=0,'扫描覆盖数量无效')
    _require(cov['universeTotal']>=cov['inScope'] and cov['supported']+cov['unsupported']==cov['inScope']
             and cov['requested']==cov['supported'] and cov['ok']+cov['failed']==cov['requested'],'扫描覆盖数量不自洽')
    _require((report['status']=='complete') == (cov['unsupported']+cov['failed']==0),'报告状态与覆盖缺口不一致')
    candidates=report.get('candidates');uncovered=report.get('uncovered')
    _require(isinstance(candidates,list) and len(candidates)<=cov['ok'],'候选数量无效')
    _require(isinstance(uncovered,list) and len(uncovered)==cov['failed']+cov['unsupported'],'未覆盖明细与数量不一致')
    seen=set()
    for item in candidates:
        _require(isinstance(item,dict),'候选记录无效')
        code=item.get('code','');exchange=item.get('exchange');name=item.get('name')
        _require(isinstance(code,str) and re.fullmatch(r'[0-9]{6}',code) and code not in seen,'候选代码重复或无效')
        seen.add(code)
        _require(isinstance(name,str) and name and 'ST' not in name.upper() and item.get('st') is False,'候选包含ST或身份信息不完整')
        _require((exchange=='SH' and code.startswith(('600','601','603','605')))
                 or (exchange=='SZ' and code.startswith(('000','001','002','003'))),'候选包含排除板块')
        _require(item.get('symbol')==code+('.SS' if exchange=='SH' else '.SZ'),'证券代码映射不一致')
        _require(_finite(item.get('close'),True),'候选收盘价格无效')
        _require(isinstance(item.get('flags'),list),'候选标记格式无效')
        signals=item.get('signals');_require(isinstance(signals,list) and signals,'候选没有B信号')
        for signal in signals:
            _require(isinstance(signal,dict) and signal.get('type') in ('B1','B2','B3')
                     and signal.get('confirmationDate')==target,'信号类别或确认日期不一致')
            _require(type(signal.get('rawIndex')) is int and signal['rawIndex']>=0,'信号确认索引无效')
            for key in ('price','stop'):
                _require(signal.get(key) is None or _finite(signal.get(key),True),'信号价格无效')


def _load_report(key, expected, now, expected_sha=None):
    _require(isinstance(key,str) and REPORT_KEY.fullmatch(key),'报告标识无效',400,'invalid_parameters')
    run=_safe_file(REPORTS_DIR,key)
    _require(run.is_dir(),'指定报告不存在',404,'not_found')
    path=_safe_file(run,'report.json');raw=_read(path);digest=_sha(raw)
    if expected_sha is not None:
        _require(isinstance(expected_sha,str) and HASH.fullmatch(expected_sha),'报告SHA参数无效',400,'invalid_parameters')
        _require(digest==expected_sha.upper(),'报告版本已变化，请刷新清单后重试',409,'report_changed')
    report=_json(raw);_validate_report(report,run,expected,now)
    return report,run,digest


def _metadata(report, run, digest, candidates=False):
    result={'reportKey':run.name,'reportSha256':digest,**{k:report.get(k) for k in REPORT_FIELDS}}
    if candidates:
        result['candidates']=report['candidates']
    return result


def _progress(now):
    out={'freshness':'unknown'}
    try:
        raw=_json(_read(_safe_file(REPORTS_DIR,'progress.json'),64*1024))
        if not isinstance(raw,dict):return out
        out={**{k:raw[k] for k in ('status','targetDate','startedAt','updatedAt','processed','requested','resumeSkipped','runDirectory') if k in raw},'freshness':'unknown'}
        age=(now-_aware(raw.get('updatedAt'))).total_seconds()
        out['freshness']='recent' if -30<=age<=300 else 'stale'
    except (ApiError,OSError,ValueError,TypeError):
        pass
    return out


def get_daily_signals():
    now=now_cn()
    result={'schema':'chanxu-daily-signals-v1','marketStatus':None,'expectedTargetDate':None,
            'reportState':'missing','report':None,
            'summary':{'candidateCount':0,'signalCount':0,'typeCounts':{'B1':0,'B2':0,'B3':0},'flaggedCount':0,'uncoveredReasons':[]},
            'progress':_progress(now)}
    try:
        market=market_status(now)
        result.update(marketStatus=market,expectedTargetDate=market['targetDate'])
    except (ValueError,TypeError):
        result.update(reportState='calendar_unavailable',error='交易日历不可用，未猜测最新交易日')
        return result
    try:
        latest_path=_safe_file(REPORTS_DIR,'latest.json')
        if not latest_path.exists():return result
        latest_raw=_read(latest_path);latest=_json(latest_raw)
        _require(isinstance(latest,dict),'最新报告格式无效')
        run=Path(latest.get('runDirectory',''))
        _require(run.is_absolute() and run.resolve().is_relative_to(REPORTS_DIR.resolve()),'最新报告目录无效')
        report,run,digest=_load_report(run.name,market['targetDate'],now)
        _require(_sha(latest_raw)==digest,'最新清单与日期报告版本不同，请稍后刷新',409,'report_changed')
        counts=Counter(s['type'] for c in report['candidates'] for s in c['signals'])
        reasons=Counter(str(x.get('error') or x.get('reason') or x.get('status') or '未覆盖')[:240] for x in report['uncovered'])
        result.update(reportState='current' if report['targetDate']==market['targetDate'] else 'outdated',
                      report=_metadata(report,run,digest,True),
                      summary={'candidateCount':len(report['candidates']),'signalCount':sum(counts.values()),
                               'typeCounts':{k:counts[k] for k in ('B1','B2','B3')},
                               'flaggedCount':sum(bool(c['flags']) for c in report['candidates']),
                               'uncoveredReasons':[{'reason':k,'count':v} for k,v in reasons.most_common()]})
    except ApiError as exc:
        result.update(reportState='invalid',error=str(exc))
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        result.update(reportState='invalid',error='报告文件无效或暂时不可读取')
    return result


def get_snapshot(report_key, report_sha256, code):
    _require(isinstance(code,str) and re.fullmatch(r'[0-9]{6}',code),'股票代码必须为六位数字',400,'invalid_parameters')
    now=now_cn()
    try:
        expected=market_status(now)['targetDate']
    except (ValueError,TypeError):
        raise ApiError('交易日历不可用，无法核对快照日期',503,'calendar_unavailable')
    report,run,digest=_load_report(report_key,expected,now,report_sha256)
    candidate=next((c for c in report['candidates'] if c['code']==code),None)
    _require(candidate is not None,'该股票不在指定报告的候选清单内',404,'not_candidate')
    _require(_sha(_read(PROJECT_DIR/'engine.js',4*1024*1024))==report['engineSha256'].upper(),
             '当前交易引擎与报告版本不兼容，拒绝混用快照；请使用对应引擎版本复核',409,'engine_incompatible')
    checkpoint_path=_safe_file(run,'results.jsonl')
    checkpoint=_read(checkpoint_path,64*1024*1024);row=None
    for line in checkpoint.splitlines():
        if line.strip():
            record=_json(line)
            _require(isinstance(record,dict),'快照检查点格式无效')
            if record.get('code')==code:row=record
    _require(row is not None and row.get('status')=='ok','指定股票最新计算记录未成功',409,'invalid_snapshot')
    _require(row.get('targetDate')==report['targetDate'] and row.get('asOf')==report['targetDate']
             and row.get('engineSha256')==report['engineSha256'] and row.get('pipelineSha256')==report['pipelineSha256'],
             '快照计算日期或原始版本不一致',409,'invalid_snapshot')
    for key in ('name','symbol','exchange','close','signals'):
        _require(row.get(key)==candidate.get(key),'快照与报告候选证据不一致',409,'invalid_snapshot')
    input_path=_safe_file(run,'inputs/'+code+'.json.gz')
    try:
        with gzip.open(input_path,'rb') as stream:input_raw=stream.read(8*1024*1024+1)
    except (OSError,EOFError):
        raise ApiError('原始行情快照缺失或损坏',409,'invalid_snapshot')
    _require(len(input_raw)<=8*1024*1024 and _sha(input_raw)==row.get('inputSha256'),
             '原始行情快照SHA校验失败',409,'input_changed')
    payload=_json(input_raw);bars=payload.get('bars');meta=payload.get('meta')
    _require(isinstance(bars,list) and len(bars)>=5 and isinstance(meta,dict),'原始行情快照格式无效',409,'invalid_snapshot')
    _require(meta==row.get('dataMeta') and meta.get('source')=='Yahoo Finance chart API',
             '原始行情来源元信息与计算记录不一致',409,'invalid_snapshot')
    _require(meta.get('symbol')==candidate['symbol'] and meta.get('range')=='2y' and meta.get('interval')=='1d',
             '原始行情证券或周期不一致',409,'invalid_snapshot')
    _require(meta.get('adjustment')=='unadjusted OHLC; adjusted close is not applied' and meta.get('stale') is not True,
             '原始行情价格口径不一致或被标记过期',409,'invalid_snapshot')
    _require(row.get('barCount')==len(bars) and candidate.get('barCount')==len(bars),
             '原始行情根数与计算记录不一致',409,'invalid_snapshot')
    previous=None
    for bar in bars:
        _require(isinstance(bar,dict),'日K格式无效',409,'invalid_snapshot')
        day=bar.get('time');_day(day)
        _require((previous is None or day>previous) and day<=report['targetDate'],'快照日期重复、倒序或包含目标日后的行情',409,'invalid_snapshot')
        previous=day
        _require(all(_finite(bar.get(k),True) for k in ('open','high','low','close'))
                 and _finite(bar.get('volume')) and bar['volume']>=0
                 and bar['low']<=min(bar['open'],bar['close'])<=max(bar['open'],bar['close'])<=bar['high'],
                 '快照OHLCV无效',409,'invalid_snapshot')
    _require(previous==report['targetDate'] and meta.get('asOf')==report['targetDate'],'快照未覆盖目标日',409,'invalid_snapshot')
    _require(bars[-1]['close']==row.get('close')==candidate.get('close')
             and bars[-1]['volume']==row.get('volume')==candidate.get('volume'),
             '原始行情收盘价或成交量与清单显示值不一致',409,'invalid_snapshot')
    for signal in candidate['signals']:
        index=signal['rawIndex'];pivot=signal.get('rawPivotIndex')
        _require(index<len(bars) and bars[index]['time']==signal['confirmationDate']==report['targetDate'],
                 '快照信号确认日与原始索引不一致',409,'invalid_snapshot')
        if pivot is not None:
            _require(type(pivot) is int and 0<=pivot<=index and bars[pivot]['time']==signal.get('pivotDate'),
                     '快照转折日与原始索引不一致',409,'invalid_snapshot')
    _require(_sha(_read(_safe_file(run,'report.json')))==digest
             and _sha(_read(checkpoint_path,64*1024*1024))==_sha(checkpoint),
             '读取期间报告或检查点发生变化，请刷新重试',409,'report_changed')
    return {'bars':bars,'meta':meta,'candidate':candidate,'report':_metadata(report,run,digest),
            'engineCompatible':True}
