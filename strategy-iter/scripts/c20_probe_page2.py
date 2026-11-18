# -*- coding: utf-8 -*-
import json
import sys
import time

sys.path.insert(0, "scripts")
from c20_mcp import MCP

m = MCP()


def run(query, limit=100, page=1):
    t0 = time.time()
    r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": query,
                                "limit": limit, "page": page}, timeout=300)
    txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
    o = json.loads(txt) if txt.strip() else {}
    return o, time.time() - t0


Q = "2026年9月30日 主力资金流向 特大单净买入额 中单净买入额 小单净买入额"
for lim in (100, 200):
    o, dt = run(Q, limit=lim, page=1)
    d = o.get("data") or {}
    codes = [x["股票代码"] for x in (d.get("datas") or [])]
    print(f"limit={lim} ok={o.get('success')} rows={d.get('row_count')} got={len(codes)} {dt:.1f}s", flush=True)
    if codes:
        o2, dt2 = run(Q, limit=lim, page=2)
        d2 = o2.get("data") or {}
        c2 = [x["股票代码"] for x in (d2.get("datas") or [])]
        print(f"   page2 got={len(c2)} first={c2[:2]} overlap={len(set(codes) & set(c2))}", flush=True)
