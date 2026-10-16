# -*- coding: utf-8 -*-
"""C15 样本外复审的共享只读工具（2026-10-16）。

纪律（与 runs/ 保留规则一致）：
- 只读 data/recap 快照与 pool_track.json，不写任何生产文件；
- 研究产物只落 runs/c15_oos_review/；
- 交易日历取本地行情库（raw_kline_daily）为权威来源，不用「面板最后一行」近似
  ——后者正是 2026-09-29 重验 09-24 池的成因（见 audit 报告）。
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
BASE = HERE.parent.parent.parent           # 仓库根
SNAP_DIR = BASE / "data" / "recap"
TRACK = SNAP_DIR / "pool_track.json"
OUT_DIR = BASE / "strategy-iter" / "runs" / "c15_oos_review"

sys.path.insert(0, str(BASE / "backend"))
sys.path.insert(0, str(BASE / "backend" / "recap"))
sys.path.insert(0, str(BASE / "strategy-iter" / "scripts"))


# ---------------------------------------------------------------- 交易日历

def trade_calendar(start=None, end=None):
    """权威交易日序列（本地行情库 raw_kline_daily 的 distinct date）。

    库不可用时显式报错，不静默退回文件名推测——宁可失败，不虚构日历。
    """
    import duckdb
    import common  # strategy-iter/scripts/common.py（local_db 单一来源）
    con = duckdb.connect(common.local_db(), read_only=True)
    try:
        sql = "SELECT DISTINCT strftime(date,'%Y-%m-%d') d FROM raw_kline_daily"
        args, where = [], []
        if start:
            where.append("date >= CAST(? AS DATE)")
            args.append(start)
        if end:
            where.append("date <= CAST(? AS DATE)")
            args.append(end)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY 1"
        return [r[0] for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def next_trade_day(calendar, day_iso):
    """日历中严格晚于 day_iso 的首个交易日（ISO 字符串）。"""
    for d in calendar:
        if d > day_iso:
            return d
    return None


def prev_trade_day(calendar, day_iso):
    """日历中严格早于 day_iso 的最后一个交易日。"""
    prev = None
    for d in calendar:
        if d < day_iso:
            prev = d
        else:
            break
    return prev


# ---------------------------------------------------------------- 快照

def snap_dates():
    """data/recap 下已存在的快照日期（date8，升序）。"""
    out = []
    for p in SNAP_DIR.iterdir():
        n = p.name
        if n[:8].isdigit() and len(n[:8]) == 8 and (n.endswith(".json") or n.endswith(".json.gz")):
            out.append(n[:8])
    return sorted(out)


def load_snap(date8):
    """读快照（.json / .json.gz 双格式）；缺失返回 None。"""
    for name in (f"{date8}.json", f"{date8}.json.gz"):
        p = SNAP_DIR / name
        if not p.exists():
            continue
        raw = p.read_bytes()
        if name.endswith(".gz"):
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
    return None


def snap_pool(date8):
    """快照 → (pool dict, note 首段级别)；无快照或无池返回 (None, None)。

    与 spec_validate.validate_prev_pool 取字段路径一致
    （modules.speculation.data.pool），保证复审读的就是生产写的那份。
    """
    snap = load_snap(date8)
    if not snap:
        return None, None
    mod = ((snap.get("modules") or {}).get("speculation") or {})
    pool = ((mod.get("data") or {}).get("pool")) or None
    if not pool:
        return None, None
    note0 = str(pool.get("note") or "")
    rules = ("C7" if note0.startswith("C7")
             else "C_Final" if "C_Final" in note0
             else "M_Final" if "M_Final" in note0 else "V3")
    return pool, rules


def snap_spec(date8):
    """快照 → speculation.data（含 cycle/themes/breadth 等选择日可用特征）。"""
    snap = load_snap(date8)
    if not snap:
        return {}
    return ((snap.get("modules") or {}).get("speculation") or {}).get("data") or {}


# ---------------------------------------------------------------- 台账

def load_track():
    with open(TRACK, "r", encoding="utf-8") as f:
        return json.load(f)


def iso(date8):
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"


# ---------------------------------------------------------------- 日线

def daily_bars(codes, d_from, d_to):
    """复权日线（v_daily_qfq，与 spec_duckdb 主源同视图）：{code6: {date_iso: bar}}。

    附带 raw 未复权的 prev_close（跌停判定用，复权价无法判跌停）。
    """
    import duckdb
    import common
    import thscodes
    codes = [str(c) for c in codes if str(c)]
    if not codes:
        return {}
    tcs = {thscodes.to_thscode(c): str(c) for c in codes}
    in_sql = ", ".join("'" + t + "'" for t in sorted(tcs))
    con = duckdb.connect(common.local_db(), read_only=True)
    try:
        rows = con.execute(
            "SELECT thscode, strftime(date,'%Y-%m-%d') d, open, high, low, close, amount "
            f"FROM v_daily_qfq WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
            f"AND thscode IN ({in_sql})", [d_from, d_to]).fetchall()
        raw = con.execute(
            "SELECT thscode, strftime(date,'%Y-%m-%d') d, close, prev_close "
            f"FROM raw_kline_daily WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
            f"AND thscode IN ({in_sql})", [d_from, d_to]).fetchall()
    finally:
        con.close()
    out = {}
    for tc, d, o, h, l, c, amt in rows:
        code = tcs.get(tc)
        if code:
            out.setdefault(code, {})[d] = {"o": o, "h": h, "l": l, "c": c, "amt": amt}
    for tc, d, rc, pc in raw:
        code = tcs.get(tc)
        if code and code in out and d in out[code]:
            out[code][d]["raw_c"] = rc
            out[code][d]["raw_pc"] = pc
    return out


def write_report(name, text):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / name
    p.write_text(text, encoding="utf-8")
    return p


def write_json(name, obj):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
