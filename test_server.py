import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "test-server.log"
with LOG.open("w", encoding="utf-8") as log:
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", "8766"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
try:
    time.sleep(1)
    with urlopen("http://127.0.0.1:8766/health", timeout=5) as response:
        health = json.load(response)
    assert health["ok"] is True
    with urlopen("http://127.0.0.1:8766/api/history?symbol=510300.SS&range=6mo", timeout=30) as response:
        payload = json.load(response)
    assert len(payload["bars"]) >= 5
    assert payload["meta"]["symbol"] == "510300.SS"
    assert payload["meta"]["interval"] == "1d"
    with urlopen("http://127.0.0.1:8766/api/market-status", timeout=5) as response:
        status = json.load(response)
    assert payload["bars"][-1]["time"] <= status["targetDate"]
    assert payload["meta"]["completenessPolicy"] == "cn_close_v1"
    if payload["meta"]["readiness"] == "ready":
        assert payload["meta"]["asOf"] == status["targetDate"]
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"SMOKE OK bars={len(payload['bars'])} asOf={payload['meta']['asOf']} source={payload['meta']['source']}\n")
    print(f"SMOKE OK bars={len(payload['bars'])} asOf={payload['meta']['asOf']}")
finally:
    proc.terminate()
    proc.wait(timeout=5)
