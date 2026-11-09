"""Offline, read-only verification of a completed Chanxu daily report.

Writes only daily-report-audit.json. Never fetches market data, changes a report,
or touches the running scanner. Exit 0 = verified, exit 1 = rejected.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from screener_data import now_cn, is_trading_day
from market_clock import market_status

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'daily-report-audit.json'
POLICY = 'sh_sz_main_no_st_v2'
PIPELINE_FILES = ['engine.js', 'daily_scan_engine.js', 'screener_data.py', 'server.py', 'daily_scan.py', 'market_clock.py']
MAIN_SH = ('600', '601', '603', '605')
MAIN_SZ = ('000', '001', '002', '003')
CN = timezone(timedelta(hours=8))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest().upper()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def pipeline_hash(hashes):
    return digest('\n'.join(f'{name}:{hashes[name]}' for name in PIPELINE_FILES).encode('utf-8'))


def validate_calendar(calendar, target, expected_target=None):
    require(calendar.get('targetPolicy') == 'cn_close_v1', 'old calendar policy rejected')
    evaluated = datetime.fromisoformat(calendar['evaluatedAt'])
    require(evaluated.tzinfo is not None, 'calendar evaluation time lacks timezone')
    status = market_status(evaluated)
    expected = expected_target or market_status(now_cn())['targetDate']
    require(target == expected, f'targetDate {target} differs from expected {expected}')
    require(status['targetDate'] == target and calendar.get('latestCompletedSession') == target,
            'target is not latest completed session at recorded evaluation time')
    require(calendar.get('targetDate') == target and calendar.get('dateVerified') is True,
            'calendar evidence is absent or inconsistent')
    require(calendar.get('latestObservedTradingDate') == target, 'index latest session does not match target')
    require(calendar.get('shouldRun') is True and status['isTradingDay'], 'not a requested trading-day run')
    require(evaluated.astimezone(CN).date().isoformat() == calendar['asOf'], 'calendar date/time differs')
    return evaluated, expected


def validate_source_completion(meta, target, generated_at):
    fetched = datetime.fromisoformat(meta['fetchedAt'])
    require(fetched.tzinfo is not None, 'source fetch time lacks timezone')
    fetched = fetched.astimezone(CN)
    close_at = datetime.fromisoformat(target+'T15:00:00+08:00')
    require(close_at <= fetched <= generated_at, 'source was fetched before target close or after report')
    require(meta.get('completenessPolicy') == 'cn_close_v1' and meta.get('readiness') == 'ready'
            and meta.get('expectedThrough') == target, 'source completion evidence or target is missing')
    if fetched.date().isoformat() == target:
        source_time = meta.get('sourceRegularMarketTime')
        require(meta.get('sourceSessionConfirmed') is True and isinstance(source_time, (int, float))
                and source_time >= close_at.timestamp(), 'same-day source had not reached closing time')
    return fetched


def in_scope(row):
    code = row.get('code')
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    if row.get('st') or 'ST' in str(row.get('name', '')).upper():
        return False
    return ((row.get('exchange') == 'SH' and code.startswith(MAIN_SH))
            or (row.get('exchange') == 'SZ' and code.startswith(MAIN_SZ)))


def expected_flags(row):
    flags = []
    if row.get('volume', 0) <= 0:
        flags.append('成交量为零')
    if any(s.get('invalidatedAtClose') for s in row['signals']):
        flags.append('收盘已达失效位')
    if any(s.get('concurrentSell') for s in row['signals']):
        flags.append('同日买卖信号冲突')
    if row.get('barCount', 0) < 120:
        flags.append('历史不足120根')
    return flags


NODE_REPLAY = r'''
const readline = require('node:readline');
const E = require('./engine.js');
const adapter = require('./daily_scan_engine.js');
const rl = readline.createInterface({input:process.stdin, crlfDelay:Infinity});
rl.on('line', line => {
  try {
    const request=JSON.parse(line);
    const bars=request.bars;
    const analysis=E.analyze(bars);
    const direct=analysis.signals.filter(s=>['B1','B2','B3'].includes(s.type)&&bars[s.index].time===request.targetDate)
      .map(s=>({id:s.id,type:s.type,confirmationDate:bars[s.index].time,pivotDate:bars[s.pivotIndex]?.time||null,
        price:s.price,stop:s.stop,rawIndex:s.index,rawPivotIndex:s.pivotIndex,center:s.center||null}));
    process.stdout.write(JSON.stringify({code:request.code,direct,adapted:adapter.analyzeOne(request)})+'\n');
  } catch(error) {
    process.stdout.write(JSON.stringify({error:error.message})+'\n');
  }
});
'''


def audit(report_path, expected_target=None, no_signal_samples=12):
    report_path = Path(report_path).resolve()
    result = {'status': 'failed', 'auditedAt': now_cn().isoformat(), 'reportPath': str(report_path),
              'networkRequests': 0, 'checks': [], 'replayedCandidateCodes': [], 'replayedNoSignalCodes': []}
    try:
        report = read_json(report_path)
        require(report.get('status') in ('complete', 'partial'), 'report is not a completed full scan (sample/closed/failed rejected)')
        require(report.get('universePolicy') == POLICY, 'old or unsupported universe policy; ST/BJ report rejected')
        target = report.get('targetDate')
        calendar = report.get('calendar', {})
        evaluated_at, expected = validate_calendar(calendar, target, expected_target)
        calendar_day = evaluated_at.astimezone(CN).date()
        generated_at = datetime.fromisoformat(report['generatedAt'])
        require(generated_at.tzinfo is not None and generated_at >= evaluated_at, 'report predates its calendar evaluation')
        require(report.get('historyRange') == '2y' and report.get('adjustment') == 'unadjusted', 'unexpected history/adjustment definition')
        result.update(targetDate=target, expectedTarget=expected, historicalAudit=expected_target is not None,
                      reportStatus=report['status'])
        result['checks'].append('target is latest completed session at the recorded Beijing evaluation time')

        run_dir = Path(report['runDirectory']).resolve()
        require(run_dir.is_relative_to((ROOT / 'daily-reports').resolve()), 'report points outside project report directory')
        require(report_path.parent == run_dir or report_path.name == 'latest.json', 'report path differs from its run directory')
        evidence_paths = {'report': report_path, 'universe': run_dir / 'universe.json',
                          'checkpoint': run_dir / 'results.jsonl'}
        evidence_hashes = {name: digest(path.read_bytes()) for name, path in evidence_paths.items()}
        result['evidenceHashes'] = evidence_hashes
        hashes = {name: digest((ROOT / name).read_bytes()) for name in PIPELINE_FILES}
        require(report.get('engineSha256') == hashes['engine.js'], 'current engine hash differs from report; cannot reproduce safely')
        require(report.get('pipelineSha256') == pipeline_hash(hashes), 'current pipeline differs from report; audit refused')
        result['sourceHashes'] = hashes
        result['pipelineSha256'] = report['pipelineSha256']
        result['checks'].append('engine and all pipeline files including market clock match report version')

        universe = read_json(run_dir / 'universe.json')
        require(universe.get('universePolicy') == POLICY and universe.get('complete') is True, 'universe snapshot policy/completeness invalid')
        require(universe.get('snapshotDate') == calendar['asOf'], 'universe snapshot date differs from scan run date')
        stocks = universe['stocks']
        require(all(in_scope(s) for s in stocks), 'universe contains a disallowed board or ST security')
        stock_by_code = {s['code']: s for s in stocks}
        require(len(stock_by_code) == len(stocks), 'duplicate universe codes')
        require(universe.get('total') == len(stocks), 'universe total does not match stock records')
        require(universe.get('sourceTotal') == len(universe.get('rawStocks', [])), 'raw universe pagination count inconsistent')
        raw_stocks = universe['rawStocks']
        require(len({s.get('symbol') for s in raw_stocks}) == len(raw_stocks), 'duplicate raw universe symbols')
        expected_scope = {}
        for raw_stock in raw_stocks:
            normalized = {'code': raw_stock.get('code'), 'name': raw_stock.get('name'),
                          'exchange': str(raw_stock.get('symbol', ''))[:2].upper(), 'st': False}
            if in_scope(normalized):
                expected_scope[normalized['code']] = normalized
        require(set(expected_scope) == set(stock_by_code), 'scope snapshot omits/adds eligible raw-list stocks')
        for code, stock in stock_by_code.items():
            expected_stock = expected_scope[code]
            require(stock['name'] == expected_stock['name'] and stock['exchange'] == expected_stock['exchange'],
                    f'{code}: normalized universe does not match raw-list identity')
            expected_symbol = code + ('.SS' if stock['exchange'] == 'SH' else '.SZ')
            require(stock.get('symbol') in (None, expected_symbol), f'{code}: history symbol mapping incorrect')
        require(len(universe.get('excluded', [])) + len(stocks) == len(raw_stocks), 'universe exclusions do not reconcile')
        supported = {s['code'] for s in stocks if s.get('symbol')}
        unsupported = {s['code'] for s in stocks if not s.get('symbol')}
        require(universe['eligibleHistoryCount'] == len(supported) and universe['unsupportedHistoryCount'] == len(unsupported),
                'universe supported counts inconsistent')
        result['checks'].append('universe is unique, complete and exclusively SH/SZ mainboard non-ST')

        latest = {}
        raw_rows = 0
        historical_out_of_scope = set()
        with (run_dir / 'results.jsonl').open(encoding='utf-8') as f:
            for line_number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                raw_rows += 1
                code = row.get('code')
                require(isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit(),
                        f'checkpoint line {line_number} malformed security code')
                if code not in supported:
                    # Appended historical rows may belong to yesterday's pool (e.g. newly ST today).
                    # They are not part of the current requested set or report candidates.
                    historical_out_of_scope.add(code)
                    continue
                latest[code] = row
        require(set(latest) == supported, 'checkpoint does not cover every requested stock')
        statuses = Counter(r['status'] for r in latest.values())
        coverage = report['coverage']
        expected_counts = dict(universeTotal=universe['sourceTotal'], inScope=len(stocks), supported=len(supported),
                               requested=len(supported), ok=statuses['ok'], unsupported=len(unsupported),
                               failed=sum(v for k, v in statuses.items() if k != 'ok'))
        for field, value in expected_counts.items():
            require(coverage.get(field) == value, f'coverage.{field} mismatch: report={coverage.get(field)} actual={value}')
        require(coverage.get('statuses') == dict(statuses), 'coverage statuses differ from latest checkpoint records')
        expected_status = 'partial' if unsupported or expected_counts['failed'] else 'complete'
        require(report['status'] == expected_status, 'complete/partial label does not match actual coverage')
        uncovered = report.get('uncovered', [])
        expected_uncovered = unsupported | {c for c, r in latest.items() if r['status'] != 'ok'}
        require({r['code'] for r in uncovered} == expected_uncovered and len(uncovered) == len(expected_uncovered),
                'uncovered list missing/duplicating failures')
        result.update(coverage=expected_counts, checkpointRows=raw_rows,
                      historicalOutOfScopeCodes=sorted(historical_out_of_scope))
        result['checks'].append('coverage and complete/partial label match all latest checkpoint records')

        candidates = report.get('candidates', [])
        candidate_by_code = {r['code']: r for r in candidates}
        require(len(candidate_by_code) == len(candidates), 'duplicate report candidate codes')
        expected_candidates = {c for c, r in latest.items() if r['status'] == 'ok' and r.get('signals')}
        require(set(candidate_by_code) == expected_candidates, 'candidate list omits or adds checkpoint signal stocks')
        no_signals = sorted(c for c, r in latest.items() if r['status'] == 'ok' and not r.get('signals'))
        count = min(no_signal_samples, len(no_signals))
        sample_codes = [no_signals[(i * len(no_signals)) // count] for i in range(count)] if count else []
        replay_codes = sorted(expected_candidates) + sample_codes
        replay_inputs = {}
        checked_inputs = 0
        reanalysed_inputs = 0
        for code, row in latest.items():
            require(in_scope(row), f'{code}: result outside requested scope')
            require(row.get('targetDate') == target, f'{code}: wrong target in result')
            require(row.get('engineSha256') == report['engineSha256'] and row.get('pipelineSha256') == report['pipelineSha256'],
                    f'{code}: mixed code version in checkpoint')
            require(row.get('name') == stock_by_code[code]['name'], f'{code}: name differs from universe snapshot')
            if row.get('inputSha256'):
                with gzip.open(run_dir / 'inputs' / (code + '.json.gz'), 'rb') as f:
                    raw = f.read()
                require(digest(raw) == row['inputSha256'], f'{code}: input content hash mismatch')
                data = json.loads(raw)
                bars = data['bars']
                require(all(b['time'] <= target for b in bars), f'{code}: future input day included')
                require(not data['meta'].get('stale'), f'{code}: stale source cache used')
                require(data['meta'].get('source') == 'Yahoo Finance chart API', f'{code}: unexpected market source')
                require(data['meta'].get('adjustment', '').startswith('unadjusted'), f'{code}: adjusted source input')
                require(data['meta'].get('symbol') == stock_by_code[code]['symbol'], f'{code}: input has different security symbol')
                require(data['meta'].get('range') == '2y' and data['meta'].get('interval') == '1d', f'{code}: input history/window differs')
                fetched_at = validate_source_completion(data['meta'], target, generated_at)
                require(data['meta'] == row.get('dataMeta'), f'{code}: source metadata differs from saved input')
                require(len(bars) == row.get('barCount'), f'{code}: barCount differs from saved input')
                require(row.get('asOf') == (bars[-1]['time'] if bars else None), f'{code}: asOf differs from saved input')
                provenance = row.get('inputProvenance')
                if provenance:
                    require(provenance.get('method') == 'verified_saved_input_reanalysis', f'{code}: unknown saved-input provenance')
                    source_run = Path(provenance['sourceRun']).resolve()
                    require(source_run != run_dir and source_run.is_relative_to((ROOT / 'daily-reports').resolve()),
                            f'{code}: invalid source run in saved-input provenance')
                    require(provenance.get('sourceInputSha256') == row['inputSha256']
                            and provenance.get('originalFetchedAt') == data['meta']['fetchedAt'],
                            f'{code}: saved-input provenance has altered hash/time')
                    replayed_at = datetime.fromisoformat(provenance['replayedAt'])
                    require(replayed_at.tzinfo is not None and fetched_at <= replayed_at <= generated_at,
                            f'{code}: saved-input replay time is outside source/report bounds')
                    with gzip.open(source_run / 'inputs' / (code + '.json.gz'), 'rb') as source_file:
                        require(digest(source_file.read()) == row['inputSha256'], f'{code}: source gzip differs from migrated input')
                    reanalysed_inputs += 1
                checked_inputs += 1
                if row['status'] == 'ok':
                    require(bars and bars[-1]['time'] == target, f'{code}: successful row lacks target date')
                    dates = [b['time'] for b in bars]
                    require(all(date.fromisoformat(d).isoformat() == d for d in dates)
                            and dates == sorted(set(dates)), f'{code}: successful input dates are not unique chronological days')
                    require(row.get('ruleVersion') == report.get('ruleVersion'), f'{code}: mixed declared rule version')
                    require(row.get('close') == bars[-1]['close'] and row.get('volume') == bars[-1]['volume'], f'{code}: close/volume changed')
                    require(row.get('turnoverProxyCNY') == bars[-1]['close'] * bars[-1]['volume'], f'{code}: liquidity proxy changed')
                if code in replay_codes:
                    replay_inputs[code] = {**stock_by_code[code], 'bars': bars, 'targetDate': target}
            else:
                require(row['status'] == 'fetch_error', f'{code}: input evidence missing for a non-fetch-error result')
            if code in candidate_by_code:
                candidate = candidate_by_code[code]
                require(candidate.get('signals') == row['signals'], f'{code}: report signals differ from checkpoint')
                require(candidate.get('flags') == expected_flags(row), f'{code}: report flags differ from rules')
                for field in ('code', 'name', 'symbol', 'exchange', 'board', 'st', 'close', 'volume', 'turnoverProxyCNY', 'barCount'):
                    require(candidate.get(field) == row.get(field), f'{code}: report {field} differs from checkpoint')
                require(all(s['confirmationDate'] == target and s['type'] in ('B1', 'B2', 'B3') for s in row['signals']),
                        f'{code}: candidate signal date/type invalid')
        expected_order = sorted(candidates, key=lambda r: (bool(r['flags']), -(r.get('turnoverProxyCNY') or 0), r['code']))
        require(candidates == expected_order, 'presentation order is not deterministic liquidity/flag order')
        result['inputFilesVerified'] = checked_inputs
        result['reanalysedInputFilesVerified'] = reanalysed_inputs
        result['checks'].append('all input gzip hashes, data dates, source metadata and candidate evidence verified')

        node = shutil.which('node')
        require(node, 'Node.js is required for independent engine replay')
        requests = ''.join(json.dumps(replay_inputs[c], ensure_ascii=False) + '\n' for c in replay_codes)
        replay = subprocess.run([node, '-e', NODE_REPLAY], input=requests, cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', timeout=max(120, 3 * len(replay_codes)))
        require(replay.returncode == 0, 'engine replay process failed: ' + replay.stderr[:1000])
        replayed = [json.loads(line) for line in replay.stdout.splitlines() if line.strip()]
        require(len(replayed) == len(replay_codes), 'engine replay output count mismatch')
        direct_fields = ('id', 'type', 'confirmationDate', 'pivotDate', 'price', 'stop', 'rawIndex', 'rawPivotIndex', 'center')
        for code, output in zip(replay_codes, replayed):
            require(not output.get('error') and output.get('code') == code, f'{code}: replay error or output reordering')
            expected_signals = latest[code]['signals']
            expected_direct = [{key: s.get(key) for key in direct_fields} for s in expected_signals]
            require(output['direct'] == expected_direct, f'{code}: direct original engine replay differs from stored B points')
            adapted = output['adapted']
            require(adapted['status'] == 'ok' and adapted['signals'] == expected_signals, f'{code}: adapter replay differs from stored B points')
            require(adapted['engineSha256'] == report['engineSha256'], f'{code}: replay engine version mismatch')
            require(adapted['ruleVersion'] == report['ruleVersion'], f'{code}: replay rule label mismatch')
        after_hashes = {name: digest((ROOT / name).read_bytes()) for name in PIPELINE_FILES}
        require(after_hashes == hashes, 'pipeline changed during audit')
        require({name: digest(path.read_bytes()) for name, path in evidence_paths.items()} == evidence_hashes,
                'report, universe or checkpoint changed during audit')
        result.update(status='passed', candidateStocksVerified=len(expected_candidates),
                      candidateSignalsVerified=sum(len(latest[c]['signals']) for c in expected_candidates),
                      replayedCandidateCodes=sorted(expected_candidates), replayedNoSignalCodes=sample_codes)
        result['checks'].append('all candidates and deterministic no-signal samples match direct engine and adapter replay')
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', help='Completed report.json or daily-reports/latest.json')
    parser.add_argument('--expected-target', help='Explicit historical target YYYY-MM-DD; default verifies current latest completed trading session')
    parser.add_argument('--no-signal-samples', type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.no_signal_samples <= 100:
        parser.error('--no-signal-samples must be between 1 and 100')
    output = audit(args.report, args.expected_target, args.no_signal_samples)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0 if output['status'] == 'passed' else 1)
