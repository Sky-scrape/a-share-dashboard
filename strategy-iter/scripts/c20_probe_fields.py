# -*- coding: utf-8 -*-
"""探测：特大单/中单/小单能否也拿到逐日列（此前只返回区间汇总）。"""
import json
import sys
import time

sys.path.insert(0, "scripts")
from c20_mcp import MCP

m = MCP()

CASES = [
    ("单字段-特大单", "2026年9月1日至2026年9月30日 各交易日特大单净买入额"),
    ("单字段-小单", "2026年9月1日至2026年9月30日 各交易日小单净买入额"),
    ("两字段", "2026年9月1日至2026年9月30日 各交易日特大单净买入额 小单净买入额"),
    ("主力+特大", "2026年9月1日至2026年9月30日 各交易日主力资金流向 特大单净买入额"),
]

for tag, q in CASES:
    t0 = time.time()
    r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": q,
                                "limit": 2, "page": 1}, timeout=180)
    txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
    o = json.loads(txt) if txt.strip() else {}
    d = o.get("data") or {}
    cols = [c["key"] for c in (d.get("columns") or [])]
    daily = [c for c in cols if "[" in c and len(c.split("[")[1].rstrip("]")) == 8]
    agg = [c for c in cols if "[" in c and "-" in c.split("[")[1]]
    print(f"{tag:14s} ok={o.get('success')} cols={len(cols)} 逐日={len(daily)} 区间汇总={len(agg)} "
          f"{time.time()-t0:.1f}s  err={str(o.get('error'))[:60]}", flush=True)
    if agg:
        print(f"   汇总列示例: {agg[:3]}", flush=True)
    if daily:
        print(f"   逐日列示例: {daily[:3]}", flush=True)
    time.sleep(3)
