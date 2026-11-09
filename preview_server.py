"""Separate localhost preview using explicit offline fixtures, never live quotes."""
import argparse
import copy
import json
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


def main():
    parser = argparse.ArgumentParser(description="缠序修复版离线预览，不是实时行情")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    fixture_path = Path(__file__).resolve().parent / "preview-fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def history(raw_symbol, period, refresh=False):
        symbol = server.normalize_symbol(raw_symbol)
        if period not in server.RANGES:
            raise server.DataError("历史范围无效")
        if symbol == "FAIL":
            raise server.DataError("离线验收：模拟行情源失败，当前数据应保留；可重试 510300.SS")
        if symbol == "SLOW":
            time.sleep(1.2)
        if symbol == "FAST":
            time.sleep(0.05)
        result = copy.deepcopy(fixture)
        if symbol == "AAPL":
            for bar in result["bars"]:
                for field in ["open", "high", "low", "close"]:
                    bar[field] = round(bar[field] * 40, 3)
        if period == "6mo":
            result["bars"] = result["bars"][-120:]
        elif period == "1y":
            result["bars"] = result["bars"][-240:]
        result["meta"] = {**result.get("meta", {}), "symbol": symbol,
                          "name": "沪深300ETF 固定验收快照" if symbol == "510300.SS" else f"{symbol} 离线验收样例",
                          "source": "离线固定验收数据（非实时）", "currency": "USD" if symbol == "AAPL" else "CNY",
                          "range": period, "asOf": result["bars"][-1]["time"], "cache": "fixture",
                          "stale": symbol == "STALE", "cacheAgeSeconds": 7200 if symbol == "STALE" else 0,
                          "fetchedAt": datetime.now(timezone.utc).isoformat(),
                          "completenessPolicy": server.COMPLETENESS_POLICY, "completenessNote": server.COMPLETENESS_NOTE}
        return result

    server.get_history = history
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), server.Handler)
    print(f"缠序修复版离线验收预览：http://127.0.0.1:{args.port}/（固定历史数据，非实时）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
