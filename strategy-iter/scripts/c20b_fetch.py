# -*- coding: utf-8 -*-
"""C20b 取数：补齐 9-10 月主力资金流，并取 7-10 月特大单/小单逐日。

上一轮踩到的两个坑（已修正）：
1. 4 个字段一起查会被降级成「区间汇总列」（[20260701-20260731]），拿不到逐日。
   → 单查或两查才有逐日列。
2. 请求太密会返回 HTTP 401「您今天的次数已用完」，但实际额度仍在（权益页显示 100%）。
   → 这是限流伪装成额度耗尽。本脚本加节流 + 长退避，不再把 401 当额度用完。

降级判定：连续 3 个请求都拿到同样的逐日列就算正常；若发现区间汇总列直接报错退出，
不写脏数据。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c20_mcp import MCP

OUT = Path(__file__).resolve().parent.parent / "raw" / "moneyflow2"
OUT.mkdir(parents=True, exist_ok=True)

MONTHS = [("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"),
          ("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-30")]
SCOPE = "非ST 非创业板 非科创板 非北交所"

# (文件名前缀, 字段列表)  —— 每项一次查询，两字段以内才能拿到逐日
TASKS = [
    ("flow2", ["特大单净买入额", "小单净买入额"]),
    ("mf", ["主力资金流向"]),
]

PACE = 0.9          # 每页间隔
BACKOFF = (25, 60, 120)
DEADLINE = None     # 全局截止（time.time() 值），到点就落盘退出，下轮续跑


class Limiter:
    def __init__(self):
        self.hits = 0

    def note(self):
        self.hits += 1


def call(m, q, page, lim, tries=5):
    for i in range(tries):
        if DEADLINE and time.time() > DEADLINE:
            raise TimeoutError("deadline")
        try:
            r = m.call("wencai_query", {"skillId": "hithink-market-query", "query": q,
                                        "limit": 100, "page": page}, timeout=300)
            txt = "".join(p.get("text", "") for p in r.get("content", [])
                          if p.get("type") == "text")
            if not txt.strip():
                raise ValueError("empty body")
            o = json.loads(txt)
            if not o.get("success"):
                err = str(o.get("error") or "")
                if "次数已用完" in err or "401" in err:
                    lim.note()
                    wait = BACKOFF[min(i, len(BACKOFF) - 1)]
                    print(f"    限流，等 {wait}s 重试 (第{i+1}次)", flush=True)
                    time.sleep(wait)
                    continue
                raise ValueError(err[:100])
            return o["data"]
        except ValueError:
            raise
        except Exception as e:
            if i == tries - 1:
                print(f"    page{page} 放弃: {repr(e)[:90]}", flush=True)
                return None
            time.sleep(3 * (i + 1))
    return None


def check_daily(cols, fields):
    """确认返回的是逐日列而非区间汇总。"""
    keys = [c["key"] for c in (cols or [])]
    daily = [k for k in keys if "[" in k and len(k.split("[")[1].rstrip("]")) == 8]
    agg = [k for k in keys if "[" in k and "-" in k.split("[")[1]]
    if agg:
        raise SystemExit(f"返回区间汇总列，字段过多被降级: {agg[:2]}")
    return len(daily)


def fetch(m, lim, prefix, fields, start, end):
    tag = f"{prefix}_{start.replace('-', '')}_{end.replace('-', '')}"
    f = OUT / f"{tag}.json"
    part = OUT / f"{tag}.part.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {tag} 已有 {len(d.get('datas') or [])} 行", flush=True)
        return d
    q = f"{start}至{end} 各交易日{' '.join(fields)} {SCOPE}"
    rows, cols, total, page = [], None, None, 1
    if part.exists():
        p = json.loads(part.read_text(encoding="utf-8"))
        rows, cols, total = p["rows"], p["cols"], p.get("total")
        page = p.get("next", 1)
        print(f"  {tag} 续跑，已有 {len(rows)} 行，从 page{page} 开始", flush=True)
    while True:
        d = call(m, q, page, lim)
        if d is None:
            part.write_text(json.dumps({"rows": rows, "cols": cols, "total": total,
                                        "next": page}, ensure_ascii=False), encoding="utf-8")
            print(f"  {tag} 在 page{page} 中断，已存 {len(rows)} 行", flush=True)
            return None
        if cols is None:
            n = check_daily(d.get("columns"), fields)
            if n == 0:
                raise SystemExit(f"{tag} 无逐日列: "
                                 f"{[c['key'] for c in d.get('columns') or []]}")
            print(f"  {tag} 逐日列 {n} 个", flush=True)
        cols = d.get("columns") or cols
        total = d.get("row_count") or total
        batch = d.get("datas") or []
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if total and page * 100 > total + 100:
            break
        time.sleep(PACE)
    out = {"columns": cols, "datas": rows, "row_count": total, "query": q}
    f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    if part.exists():
        part.unlink()
    print(f"  {tag} {len(rows)}/{total} 行, {page} 页", flush=True)
    return out


if __name__ == "__main__":
    import os
    m = MCP()
    lim = Limiter()
    budget = float(os.environ.get("C20B_BUDGET", "100"))
    DEADLINE = time.time() + budget
    t0 = time.time()
    try:
        for prefix, fields in TASKS:
            for s, e in MONTHS:
                if prefix == "mf" and e < "2026-08-31":
                    continue    # 7-8 月主力已在 c20_fetch_moneyflow 取过
                fetch(m, lim, prefix, fields, s, e)
    except TimeoutError:
        print("时间预算到，落盘退出", flush=True)
    print(f"DONE {time.time()-t0:.0f}s, 限流 {lim.hits} 次 -> {OUT}")
