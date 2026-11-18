# -*- coding: utf-8 -*-
"""C19：抓同花顺两个可用且有历史的接口（7-10 月）。

- limit-up-statistics：封板率 / 炸板数 / 涨停数 / 各板级家数与晋级率 → 市场情绪（择时）
- auction-stock-opportunities：竞价抢筹/砸盘信号（含 grade、竞价额 vs 前日成交额）

这两个都不在「同花顺AI客户端专用」名单里，实测 API Key 可调（code=0）。
筹码形态 / 主力资金 / 高频分时均返回 code=2004（客户端专用），不可用。
"""
import json
import os
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "raw" / "thscache"
OUT.mkdir(parents=True, exist_ok=True)

KEY_FILE = os.path.join(os.environ.get('APPDATA', ''), 'hithink-finance', 'credentials.env')
KEY = None
for line in Path(KEY_FILE).read_text(encoding='utf-8').splitlines():
    if line.startswith('HITHINK_FINANCE_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
BASE_URL = "https://fuyao.aicubes.cn/api/a-share/special-data"
H = {"X-api-key": KEY}

td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
days = [d for d in td if "2026-07-01" <= d <= "2026-10-30"]
print(f"目标 {len(days)} 个交易日")


def grab(name, path, params):
    f = OUT / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    r = requests.get(f"{BASE_URL}/{path}", params=params, headers=H, timeout=25)
    d = r.json()
    if d.get("code") != 0:
        print(f"  {name} FAIL {d.get('code')} {d.get('message')}")
        return None
    f.write_text(json.dumps(d["data"], ensure_ascii=False), encoding="utf-8")
    return d["data"]


for i, d in enumerate(days, 1):
    a = grab(f"lu_stat_{d.replace('-', '')}", "limit-up-statistics", {"date": d})
    b = grab(f"auc_{d.replace('-', '')}", "auction-stock-opportunities", {"date": d})
    na = len((a or {}).get('exact_boards') or []) if a else 0
    nb = len((b or {}).get('item') or []) if b else 0
    if i % 10 == 1:
        print(f"[{i}/{len(days)}] {d} lu_stat={'ok' if a else 'FAIL'} auc_n={nb}", flush=True)

print("DONE")
