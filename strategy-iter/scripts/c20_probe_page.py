# -*- coding: utf-8 -*-
import json
import sys
import time

sys.path.insert(0, "scripts")
from c20_mcp import MCP

m = MCP()
for lim in (200, 500, 1000):
    t0 = time.time()
    try:
        r = m.call("wencai_query", {"skillId": "hithink-market-query",
                                    "query": "2026年10月30日 主力资金流向 特大单净买入额 小单净买入额",
                                    "limit": lim, "page": 1}, timeout=300)
        txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
        obj = json.loads(txt)
        d = obj.get("data") or {}
        print(f"limit={lim} [{time.time()-t0:.1f}s] ok={obj.get('success')} "
              f"rows={d.get('row_count')} got={len(d.get('datas') or [])} "
              f"keys={list((d.get('datas') or [{}])[0].keys())}", flush=True)
    except Exception as e:
        print(f"limit={lim} EXC {repr(e)[:150]}", flush=True)
