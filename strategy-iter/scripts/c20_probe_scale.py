# -*- coding: utf-8 -*-
"""探测量级：不同窗口 × 字段数，找能稳定返回的最长窗口。"""
import json
import sys
import time

sys.path.insert(0, "scripts")
from c20_mcp import MCP

CASES = [
    ("2026年7月1日至2026年7月31日 各交易日主力资金流向 特大单净买入额 小单净买入额", 30),
    ("2026年7月1日至2026年8月31日 各交易日主力资金流向 特大单净买入额", 30),
    ("2026年7月1日至2026年10月30日 各交易日主力资金流向", 30),
]

m = MCP()
for q, lim in CASES:
    t0 = time.time()
    try:
        r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": q,
                                    "limit": lim, "page": 1}, timeout=300)
        txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
        obj = json.loads(txt)
        d = obj.get("data") or {}
        print(f"[{time.time()-t0:5.1f}s] {q[:34]}... success={obj.get('success')} "
              f"rows={d.get('row_count')} cols={len(d.get('columns') or [])} "
              f"got={len(d.get('datas') or [])} err={obj.get('error')}", flush=True)
    except Exception as e:
        print(f"[{time.time()-t0:5.1f}s] {q[:34]}... EXC {repr(e)[:120]}", flush=True)
