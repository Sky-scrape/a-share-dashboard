# -*- coding: utf-8 -*-
import json
import sys

sys.path.insert(0, "scripts")
from c20_mcp import MCP

m = MCP()
r = m.call("wencai_query", {"skillId": "hithink-market-query",
                            "query": "2026年9月30日 主力资金流向 特大单净买入额",
                            "limit": 3, "page": 1}, timeout=120)
txt = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
o = json.loads(txt) if txt.strip() else {}
d = o.get("data") or {}
print("success:", o.get("success"))
print("error:", o.get("error"))
print("rows:", d.get("row_count"), "got:", len(d.get("datas") or []))
if d.get("datas"):
    print("sample:", {k: v for k, v in list(d["datas"][0].items())[:5]})
