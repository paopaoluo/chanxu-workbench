"""Reanalyse verified same-day saved market inputs after a universe policy change.

Does not reuse old signals, change source timestamps, change cache TTL, or publish
a report. The regular daily scanner resumes these freshly computed checkpoints.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import gzip
import json
from pathlib import Path
import shutil

import daily_scan as scan
from screener_data import load_universe, now_cn


def latest_rows(path):
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
            rows[row['code']] = row
        except (ValueError, KeyError):
            continue
    return rows


def replay(source):
    with scan.scan_lock():
        source = source.resolve()
        source.relative_to(scan.REPORTS.resolve())
        old_report = json.loads((source/'report.json').read_text(encoding='utf-8'))
        today = now_cn().date().isoformat()
        target = old_report['targetDate']
        assert old_report['calendar']['asOf'] == today, 'source report is not from today'
        assert old_report['calendar']['dateVerified'], 'source target date was not verified'
        assert old_report['calendar']['latestObservedTradingDate'] == target
        assert old_report['status'] in ('complete', 'partial'), 'sample inputs are not migrated'
        universe = load_universe()
        assert universe['universePolicy'] == 'sh_sz_main_no_st_v2'
        stocks = {s['code']: s for s in universe['stocks']}
        engine_hash = scan.sha((scan.ROOT/'engine.js').read_bytes())
        pipeline_hash = scan.pipeline_sha()
        assert old_report['engineSha256'] == engine_hash
        dest = scan.REPORTS/f'{target}-{engine_hash[:12]}-{pipeline_hash[:8]}'
        assert dest != source and dest.is_dir(), 'expected current run directory is absent'
        checkpoint = dest/'results.jsonl'
        current = latest_rows(checkpoint) if checkpoint.exists() else {}
        retained = {c for c, r in current.items()
                    if scan.valid_checkpoint(r, target, engine_hash, pipeline_hash, dest)}
        old_rows = latest_rows(source/'results.jsonl')
        migrated, rejected = [], []
        inputs = dest/'inputs'
        inputs.mkdir(exist_ok=True)
        started = now_cn().isoformat()
        with scan.Bridge() as bridge, checkpoint.open('a', encoding='utf-8') as log:
            for code, old in old_rows.items():
                if code not in stocks or code in retained or old.get('status') != 'ok':
                    continue
                try:
                    stock = stocks[code]
                    assert old['engineSha256'] == engine_hash
                    assert old['symbol'] == stock['symbol']
                    assert old['targetDate'] == target and old['asOf'] == target
                    old_input = source/'inputs'/(code+'.json.gz')
                    raw = gzip.open(old_input, 'rb').read()
                    assert scan.sha(raw) == old['inputSha256'], 'raw input SHA mismatch'
                    payload = json.loads(raw)
                    meta, bars = payload['meta'], payload['bars']
                    assert meta['symbol'] == stock['symbol']
                    assert meta['source'] == 'Yahoo Finance chart API'
                    assert meta['range'] == '2y' and meta['interval'] == '1d'
                    assert meta['adjustment'] == 'unadjusted OHLC; adjusted close is not applied'
                    assert meta.get('stale') is False
                    assert datetime.fromisoformat(meta['fetchedAt']).astimezone(now_cn().tzinfo).date().isoformat() == today
                    dates = [b['time'] for b in bars]
                    assert dates and dates[-1] == target
                    assert all(a < b for a, b in zip(dates, dates[1:]))
                    assert all(d <= target for d in dates)
                    result = bridge.analyze(stock, bars, target)
                    assert result['status'] == 'ok' and result['asOf'] == target
                    assert result['engineSha256'] == engine_hash
                    row = {**stock, **result, 'inputSha256':scan.sha(raw), 'dataMeta':meta,
                           'volume':bars[-1].get('volume', 0),
                           'turnoverProxyCNY':bars[-1]['close']*bars[-1].get('volume', 0),
                           'pipelineSha256':pipeline_hash,
                           'inputProvenance':{'method':'verified_saved_input_reanalysis',
                               'sourceRun':str(source), 'sourceInputSha256':scan.sha(raw),
                               'originalFetchedAt':meta['fetchedAt'], 'replayedAt':now_cn().isoformat()}}
                    shutil.copyfile(old_input, inputs/(code+'.json.gz'))
                    assert scan.valid_checkpoint(row, target, engine_hash, pipeline_hash, dest)
                    log.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
                    log.flush()
                    migrated.append(code)
                    if len(migrated) % 100 == 0:
                        print(f'REPLAY {len(migrated)} retained={len(retained)}', flush=True)
                except Exception as exc:
                    rejected.append({'code':code, 'error':str(exc)})
        assert scan.pipeline_sha() == pipeline_hash, 'pipeline changed during replay'
        outcome = {'startedAt':started, 'finishedAt':now_cn().isoformat(), 'sourceRun':str(source),
                   'targetDate':target, 'runDirectory':str(dest), 'engineSha256':engine_hash,
                   'pipelineSha256':pipeline_hash, 'retainedExistingOk':len(retained),
                   'reanalysedSavedInputCount':len(migrated), 'reanalysedCodes':migrated,
                   'rejected':rejected, 'nextAction':'run daily_scan.py to resume and fetch remaining stocks'}
        scan.write_json(dest/'saved-input-replay.json', outcome)
        print(json.dumps({k:v for k,v in outcome.items() if k!='reanalysedCodes'}, ensure_ascii=False), flush=True)
        return outcome


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    replay(args.source)
