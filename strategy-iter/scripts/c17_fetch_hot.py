# -*- coding: utf-8 -*-
"""C17：抓 7-10 月历史热股榜（人气/关注度维度，同花顺 special.hot-stock-history）。

输出 raw/hot/hot_YYYYMMDD.json：{date, ranks: {thscode: rank}, total}
断点续抓：已存在的日期跳过。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
from common import run_cli  # noqa: E402

OUT = BASE / "raw" / "hot"
OUT.mkdir(parents=True, exist_ok=True)
td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
days = [d for d in td if "2026-07-01" <= d <= "2026-10-30"]
print(f"目标 {len(days)} 个交易日")

for i, d in enumerate(days, 1):
    f = OUT / f"hot_{d.replace('-', '')}.json"
    if f.exists():
        continue
    try:
        env = run_cli(["special", "hot-stock-history", "--date", d])
        items = (env.get("data") or {}).get("item") or []
        ranks = {it["thscode"]: int(it["rank"]) for it in items if it.get("thscode")}
        f.write_text(json.dumps({"date": d, "ranks": ranks, "total": len(ranks)},
                                ensure_ascii=False), encoding="utf-8")
        print(f"[{i}/{len(days)}] {d} n={len(ranks)}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{i}/{len(days)}] {d} FAIL {type(e).__name__}: {str(e)[:80]}", flush=True)
print("DONE")
