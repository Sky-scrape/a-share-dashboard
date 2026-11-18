# -*- coding: utf-8 -*-
"""C20 取数：主力资金流（四类单子日级净额），7-10 月，主板。

关键发现（本脚本的前提）：
- HANDOFF 说的「主力资金流被锁在客户端」只对 Fuyao API Key 成立。
  客户端内的问财结构化查询可以按日期区间返回宽表：一行一只票、每个交易日一列，
  相对 API 的 2004 只是权限路径不同，口径一致（主力=特大单+大单，另有中单/小单）。
- 分页 limit 上限 100，page 递增不重叠；超限返回非 JSON。
- 单次查询日期跨度太大（>约 1 个季度 × 3~4 字段）会被中断，按月切分最稳。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c20_mcp import MCP

OUT = Path(__file__).resolve().parent.parent / "raw" / "moneyflow"
OUT.mkdir(parents=True, exist_ok=True)

FIELDS = ["主力资金流向", "特大单净买入额", "中单净买入额", "小单净买入额"]
MONTHS = [("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"),
          ("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-30")]
SCOPE = "非ST 非创业板 非科创板 非北交所"


def call(m, q, page, tries=4):
    for i in range(tries):
        try:
            r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": q,
                                        "limit": 100, "page": page}, timeout=300)
            txt = "".join(p.get("text", "") for p in r.get("content", [])
                          if p.get("type") == "text")
            if not txt.strip():
                raise ValueError("empty body")
            o = json.loads(txt)
            if not o.get("success"):
                raise ValueError(str(o.get("error"))[:80])
            return o["data"]
        except Exception as e:
            if i == tries - 1:
                print(f"    page{page} give up: {repr(e)[:100]}", flush=True)
                return None
            time.sleep(1.5 * (i + 1))
    return None


def fetch_month(m, start, end):
    tag = f"{start.replace('-', '')}_{end.replace('-', '')}"
    f = OUT / f"mf_{tag}.json"
    part = OUT / f"mf_{tag}.part.json"
    rows, cols, total, page = [], None, None, 1
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {tag} cached rows={len(d.get('datas') or [])}", flush=True)
        return d
    if part.exists():
        p = json.loads(part.read_text(encoding="utf-8"))
        rows, cols, total, page = p["rows"], p["cols"], p.get("total"), p.get("next", 1)
        print(f"  {tag} resume rows={len(rows)} page={page}", flush=True)
    q = f"{start}至{end} 各交易日{' '.join(FIELDS)} {SCOPE}"
    while True:
        d = call(m, q, page)
        if d is None:
            part.write_text(json.dumps({"rows": rows, "cols": cols, "total": total,
                                        "next": page}, ensure_ascii=False), encoding="utf-8")
            print(f"  {tag} STOP at page{page}, saved {len(rows)}", flush=True)
            return None
        cols = d.get("columns") or cols
        total = d.get("row_count") or total
        batch = d.get("datas") or []
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if total and page * 100 > total + 100:
            break
    d = {"columns": cols, "datas": rows, "row_count": total, "query": q}
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if part.exists():
        part.unlink()
    print(f"  {tag} rows={len(rows)}/{total} cols={len(cols or [])} pages={page}", flush=True)
    return d


if __name__ == "__main__":
    m = MCP()
    t0 = time.time()
    for s, e in MONTHS:
        for _ in range(4):
            if fetch_month(m, s, e) is not None:
                break
    print(f"DONE {time.time()-t0:.0f}s -> {OUT}")
