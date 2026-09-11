# -*- coding: utf-8 -*-
"""一次性修复：20260910 快照四个 error 模块中可回补的部分。

背景：09-10 17:05 采集遇同花顺上游错误（UPST/超时）+ 新浪连接失败，
limit_up_pool / limit_down_pool / breadth / market_indices 四模块 error →
隔夜链终版池空仓。本脚本做同口径/近似口径数据修复（用户 2026-09-11 批准）：

  1. limit_up_pool / limit_down_pool：hithink 历史接口重抓（--date-ms 同口径），
     成交额从本地 DuckDB v_daily_qfq 回填（原口径=当日全市场快照 turnover，
     快照类当日-only 无法回补，DuckDB 日线成交额为其收盘等价）；
  2. breadth：涨停/跌停/最高连板/真实涨停/炸板 取自重抓池子与既有炸板池
     （原口径同源）；上涨/下跌/平盘 从 DuckDB 日线涨跌符号重算
     （近似口径：原=同花顺全市场快照实时涨跌幅符号）；
  3. market_indices：数据源当日-only 无法回补，保持 error 诚实降级
     （仅页面指数卡展示用，不进情绪/池子链路）。

修完后由 derive.py 重建 sentiment 面板、speculate --date 20260910 终版重算
（本脚本不跑这两步，分开跑便于核对）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))

import providers  # noqa: E402
import fsutil  # noqa: E402
from spec_duckdb import _db_export  # noqa: E402

DATE = "20260910"
ISO = f"{DATE[:4]}-{DATE[4:6]}-{DATE[6:]}"
SNAP = os.path.join(ROOT, "data", "recap", f"{DATE}.json")

with open(SNAP, encoding="utf-8") as f:
    snap = json.load(f)
mods = snap["modules"]

# ---- 1. 重抓涨停/跌停池（历史接口，同口径）----
providers.set_context(DATE, historical=True)
zt_mod = providers.limit_up_pool()
dt_mod = providers.limit_down_pool()
providers.set_context(providers.DATE, historical=False)
assert zt_mod["status"] == "ok" and dt_mod["status"] == "ok", \
    (zt_mod["status"], dt_mod["status"])
zt_rows, dt_rows = zt_mod["data"], dt_mod["data"]
print(f"重抓: 涨停 {len(zt_rows)} · 跌停 {len(dt_rows)}")

# ---- 2. DuckDB 回填成交额（元；DuckDB thscode 带 .SH/.SZ 后缀，按 6 位前缀匹配）----
amt_rows = _db_export(
    f"SELECT thscode, amount FROM v_daily_qfq WHERE date = DATE '{ISO}'",
    "repair_amt")
amt_by = {str(r["thscode"]).split(".")[0]: r.get("amount") for r in amt_rows}
filled = 0
for r in zt_rows + dt_rows:
    code = str(r.get("代码") or "")
    if amt_by.get(code):
        r["成交额"] = amt_by[code]
        filled += 1
print(f"成交额回填: {filled}/{len(zt_rows) + len(dt_rows)}")

# ---- 3. breadth 重建（涨跌家数取日线涨跌符号，模式同 day_pcts 的窗口 CTE）----
cnt = _db_export(
    "WITH w AS (SELECT thscode, date, close, "
    "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS pct "
    "FROM v_daily_qfq WHERE date >= DATE '2026-09-03') "
    "SELECT SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up, "
    "SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down, "
    "SUM(CASE WHEN pct = 0 THEN 1 ELSE 0 END) AS flat "
    f"FROM w WHERE date = DATE '{ISO}'", "repair_breadth")
c0 = (cnt or [{}])[0]
up_n, down_n, flat_n = int(c0.get("up") or 0), int(c0.get("down") or 0), int(c0.get("flat") or 0)
zb_rows = (mods.get("limit_break_pool") or {}).get("data") or []
max_lb = max((int(r.get("连板数") or 0) for r in zt_rows), default=0)
real_zt = sum(1 for r in zt_rows if not r.get("is_st") and not r.get("is_new"))
breadth = {"status": "ok", "data": {
    "上涨": up_n, "下跌": down_n, "平盘": flat_n,
    "涨停家数": len(zt_rows), "跌停家数": len(dt_rows),
    "最高连板": max_lb, "炸板家数": len(zb_rows), "真实涨停": real_zt,
}}
print("breadth:", breadth["data"])

# ---- 4. 合并写回（模块形状保持 {status, data}）----
mods["limit_up_pool"] = {"status": "ok", "data": zt_rows}
mods["limit_down_pool"] = {"status": "ok", "data": dt_rows}
mods["breadth"] = breadth
fsutil.save_json_atomic(SNAP, snap, indent=2)
print(f"已写回 {SNAP}（market_indices 保持 error 诚实降级）")
