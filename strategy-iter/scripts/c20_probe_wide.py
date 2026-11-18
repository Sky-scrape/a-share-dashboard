# -*- coding: utf-8 -*-
import json
import sys
import time

sys.path.insert(0, "scripts")
from c20_mcp import MCP

m = MCP()


def q1(query, limit=100, page=1):
    t0 = time.time()
    r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": query,
                                "limit": limit, "page": page}, timeout=300)
    txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
    if not txt.strip():
        return None, f"empty ({time.time()-t0:.1f}s)"
    obj = json.loads(txt)
    d = obj.get("data") or {}
    return d, f"ok={obj.get('success')} rows={d.get('row_count')} cols={len(d.get('columns') or [])} got={len(d.get('datas') or [])} ({time.time()-t0:.1f}s)"


tests = [
    "2026年7月1日至2026年10月30日 各交易日主力资金流向 特大单净买入额 小单净买入额",
    "2026年9月1日至2026年10月30日 各交易日主力资金流向 特大单净买入额 小单净买入额 中单净买入额",
]
for q in tests:
    d, msg = q1(q)
    print(q[:40], "->", msg, flush=True)
    if d and d.get("datas"):
        print("   first:", [k for k in d["datas"][0].keys()][:6], flush=True)
        d2, msg2 = q1(q, page=2)
        print("   page2:", msg2, ("same_codes" if d2 and d2["datas"] and d2["datas"][0]["股票代码"] != d["datas"][0]["股票代码"] else ""), flush=True)
