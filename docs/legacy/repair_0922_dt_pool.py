# -*- coding: utf-8 -*-
"""一次性修复：20260922 快照 limit_down_pool 模块 error → 补回跌停池。

背景：09-22 17:07 采集时，跌停池内 *ST亚士(603378) 触发同花顺上游
hotspot_focus 关联查询失败（FUYAO_5003, unknown market_id=22），列表级
500，跌停池整模块 error。breadth 当时按预案降级（跌停家数用全市场快照
|pct|≥9.9 近似，ST 5cm 档会漏计）。09-23 上午实测上游已恢复：同口径
--date-ms 历史接口已能返回 09-22 全量跌停池（含 *ST亚士）。

本脚本（用户 2026-09-23 批准，沿用 tests/_repair_20260910.py 先例）：
  1. limit_down_pool：hithink 历史接口重抓（--date-ms 同口径），
     成交额从本地 DuckDB v_daily_qfq 回填（快照类当日-only 无法回补，
     DuckDB 日线成交额为其收盘等价）；
  2. breadth：跌停家数改为重抓池子精确值，移除「快照近似」口径备注
     （其余字段不动——涨停/炸板/涨跌家数等当日均采集成功）；
  3. 情绪面板与投机段落不在本脚本重算：修完后分开跑
     python backend/recap/speculate.py --date 20260922
     python backend/derive.py --force
     （与 0910 修复流程一致，分开跑便于核对）。

不变式 / 严格不做的事：
  - 只改 modules.limit_down_pool 与 modules.breadth.data.跌停家数 两处；
  - 不动 fetched_at / prev / pool / speculation 等其余字段；
  - 原快照已备份为 .status/20260922.json.bak-pre-dt-repair。

用法：
    python .status/repair_0922_dt_pool.py --dry-run   # 只打印差异
    python .status/repair_0922_dt_pool.py             # 实际写回
"""
import argparse
import copy
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

DATE = "20260922"
ISO = f"{DATE[:4]}-{DATE[4:6]}-{DATE[6:]}"
SNAP = os.path.join(ROOT, "data", "recap", f"{DATE}.json")

ap = argparse.ArgumentParser(description="20260922 跌停池补抓修复")
ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写回")
args = ap.parse_args()

with open(SNAP, encoding="utf-8") as f:
    snap = json.load(f)
mods = snap["modules"]

before = mods.get("limit_down_pool", {})
print(f"修复前 limit_down_pool: status={before.get('status')} "
      f"rows={len(before.get('data') or [])} "
      f"err={str(before.get('error'))[:80]}")
old_breadth_dt = (mods.get("breadth", {}).get("data") or {}).get("跌停家数")
print(f"修复前 breadth.跌停家数(快照近似) = {old_breadth_dt}")

# ---- 1. 重抓跌停池（历史接口，同口径）----
providers.set_context(DATE, historical=True)
dt_mod = providers.limit_down_pool()
providers.set_context(providers.DATE, historical=False)
if dt_mod.get("status") != "ok":
    print(f"重抓失败，保持原快照不动: {str(dt_mod.get('error'))[:200]}")
    sys.exit(1)
dt_rows = dt_mod["data"]
print(f"重抓: 跌停 {len(dt_rows)} 只 "
      f"({', '.join(r['代码'] + ' ' + (r.get('名称') or '') for r in dt_rows[:6])} ...)")

# ---- 2. DuckDB 回填成交额（元；thscode 带 .SH/.SZ 后缀，按 6 位前缀匹配）----
amt_rows = _db_export(
    f"SELECT thscode, amount FROM v_daily_qfq WHERE date = DATE '{ISO}'",
    "repair_amt_0922")
amt_by = {str(r["thscode"]).split(".")[0]: r.get("amount") for r in amt_rows}
filled = 0
for r in dt_rows:
    code = str(r.get("代码") or "")
    if amt_by.get(code):
        r["成交额"] = amt_by[code]
        filled += 1
print(f"成交额回填: {filled}/{len(dt_rows)}")

# ---- 3. breadth 跌停家数改精确值，移除近似口径备注 ----
new_breadth = copy.deepcopy(mods.get("breadth") or {})
if new_breadth.get("status") == "ok":
    new_breadth["data"]["跌停家数"] = len(dt_rows)
    removed_note = new_breadth["data"].pop("跌停家数口径", None)
    print(f"breadth.跌停家数: {old_breadth_dt}(近似) -> {len(dt_rows)}(精确池)"
          f"；移除口径备注: {'有' if removed_note else '无'}")

if args.dry_run:
    print("[dry-run] 不写回。")
    sys.exit(0)

# ---- 4. 合并写回（模块形状保持 {status, data}）----
mods["limit_down_pool"] = {"status": "ok", "data": dt_rows}
mods["breadth"] = new_breadth
fsutil.save_json_atomic(SNAP, snap, indent=2)
print(f"已写回 {SNAP}（仅 limit_down_pool 与 breadth.跌停家数 两处）")
