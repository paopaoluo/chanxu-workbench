"""Refresh the local ETF name directory independently from per-keystroke search."""
import argparse
import json
import sys
from instrument_catalog import refresh_etfs


def main():
    parser = argparse.ArgumentParser(description='刷新沪深ETF名称目录；不下载K线、不筛选信号、不下单')
    parser.add_argument('--force', action='store_true', help='跳过24小时名称缓存')
    parser.add_argument('--timeout', type=float, default=15, help='单次目录请求超时秒数（1–60）')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error('timeout 必须在1–60秒')
    result = refresh_etfs(force=args.force, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
