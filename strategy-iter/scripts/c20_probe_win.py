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
    if not txt.strip():
        return None, f"EMPTY {time.time()-t0:.1f}s"
    o = json.loads(txt)
    d = o.get("data") or {}
    return d, (f"ok={o.get('success')} rows={d.get('row_count')} cols={len(d.get('columns') or [])} "
               f"got={len(d.get('datas') or [])} {time.time()-t0:.1f}s {o.get('error') or ''}")


cases = [
    ("M1-4f", "2026年9月1日至2026年9月30日 各交易日主力资金流向 特大单净买入额 中单净买入额 小单净买入额"),
    ("M0-2f", "2026年9月1日至2026年9月30日 各交易日主力资金流向 特大单净买入额"),
    ("H1-2f", "2026年9月1日至2026年9月15日 各交易日主力资金流向 特大单净买入额"),
    ("D-4f", "2026年9月30日 主力资金流向 特大单净买入额 中单净买入额 小单净买入额"),
]
for tag, q in cases:
    d, msg = run(q)
    print(f"{tag:6s} {msg}", flush=True)
    if d and d.get("datas"):
        print("       fields:", len(d["datas"][0]), flush=True)
