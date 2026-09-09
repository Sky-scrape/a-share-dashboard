# -*- coding: utf-8 -*-
"""C6 持有期与资金管理实验（2026-09-09）。

A. T+2 持有对照：入场口径与 backtest_capital.realtime 完全一致（涨停组开盘窗内
   买入 / 低吸组限价触及成交），区别仅在离场——基线 T+1 收盘离场 vs 持有到 T+2
   收盘（止损两日有效，路径按 T+1 -> T+2 顺序推进，无未来函数）。
B. 资金管理层变体（只缩放每日仓位，不改选股与买卖点，执行层冻结口径不触碰）：
   S0 基线（每日满仓等权） / S1 环境缩仓（defensive、freeze 0.5x） /
   S2 连败降仓（连亏 2 日后 0.5x，直至回稳日） / S3 = S1+S2。
   全部变体按双边 0.15% 费后重算（含基准），并报告回撤。
用法: cd strategy-iter && python -m scripts.exp_hold_sizing runs/us_round3_C6
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data import build_store, SEL_START, SEL_END  # noqa: E402
from scripts.backtest_capital import trade_pnl, load_validation, build_curve  # noqa: E402

FEE = 0.15  # 双边成本（%），佣金+滑点近似


def t2_frame(run_dir: str, store) -> pd.DataFrame:
    """C6 picks 的 T+2 行情（相对 T 收盘 %）。T+2 = T+1 的下一交易日。"""
    rows = load_validation(run_dir)
    q = store.qfq
    qidx = store.qfq_idx
    td = store.trade_dates
    next_map = {td[i]: td[i + 1] for i in range(len(td) - 1)}
    out = []
    for r in rows:
        t1 = r["T1"]
        t2 = next_map.get(t1)
        if t2 is None:
            continue
        key = (r["thscode"], t2)
        if key not in qidx:
            continue
        row = qidx[key]
        c0 = qidx.get((r["thscode"], t1))
        base = store.raw_idx.get((r["thscode"], t1))
        # 基准沿用 validation 的口径：相对 T 日收盘（qfq close）
        try:
            c_t = float(qidx[(r["thscode"], t1)].close)
        except Exception:  # noqa: BLE001
            continue
        if not c_t:
            continue
        out.append({**r,
                    "t2_o": (float(row.open) / c_t - 1) * 100,
                    "t2_h": (float(row.high) / c_t - 1) * 100,
                    "t2_l": (float(row.low) / c_t - 1) * 100,
                    "t2_c": (float(row.close) / c_t - 1) * 100})
    return pd.DataFrame(out)


def pnl_t2(row) -> float | None:
    """T+2 持有口径单票收益（%）。入场与 realtime 一致，离场两日止损+T+2 收盘。"""
    from engine import rules as R
    LU = {"win_min": 0.0, "win_max": 5.0, "stop": -3.0}
    NL = {"win_min": -2.0, "win_max": 3.0, "stop": -4.0}
    o, h, l, c = row["o"], row["h"], row["l"], row["c"]
    if o is None or c is None:
        return None
    # T+1 止损则与基线相同（当日离场）
    if row["group"] == "lu":
        if not (LU["win_min"] <= o <= LU["win_max"]):
            return None
        if l is not None and l < LU["stop"]:
            entry, exit_pct = o, LU["stop"]
            return (1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1
        entry, e1 = o, c                      # T+1 收盘未卖，持有
        l2, c2 = row["t2_l"], row["t2_c"]
        exit_pct = LU["stop"] if (l2 is not None and l2 < LU["stop"]) else c2
    else:
        if not (NL["win_min"] <= o <= NL["win_max"]):
            return None
        entry = max(o, 0.0)
        if h is None or h < entry:
            return None                        # 限价未成交
        if l is not None and l <= NL["stop"]:
            return (1 + NL["stop"] / 100.0) / (1 + entry / 100.0) - 1
        e1 = c                                 # T+1 收盘持有到 T+2
        l2, c2 = row["t2_l"], row["t2_c"]
        exit_pct = NL["stop"] if (l2 is not None and l2 <= NL["stop"]) else c2
    return (1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1


def sizing_curves(daily: list[dict], ms_by_date: dict) -> dict:
    """对同一 trade 流按仓位方案重算资金曲线（day_ret 为满仓日收益）。"""
    def curve(sizes):
        eq, peak, dd, streak = 1.0, 1.0, 0.0, 0
        for d in daily:
            size = sizes(d, streak)
            day = d["day_ret"] * size          # day_ret 已是小数
            eq *= 1 + day
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1)
            streak = streak + 1 if d["day_ret"] <= 0 else 0
        return eq, dd

    def s_regime(d, streak):
        r = ms_by_date.get(d["T1"], "")
        return 0.5 if r in ("defensive", "freeze") else 1.0

    def s_streak(d, streak):
        return 0.5 if streak >= 2 else 1.0

    def s_combo(d, streak):
        return min(s_regime(d, streak), s_streak(d, streak))

    out = {}
    for name, fn in (("S0_满仓", lambda d, s: 1.0), ("S1_环境缩仓", s_regime),
                     ("S2_连败降仓", s_streak), ("S3_组合", s_combo)):
        eq, dd = curve(fn)
        out[name] = dict(total=round((eq - 1) * 100, 1), max_dd=round(dd * 100, 1))
    return out


def _safe_run_dir(rd: str) -> Path:
    """argv 传入的 run 目录：限定 runs/ 一级子目录（resolve 后包含性断言）。"""
    base = (Path(__file__).resolve().parent.parent / "runs").resolve()
    p = (Path(__file__).resolve().parent.parent / rd).resolve()
    if p.parent != base:
        raise ValueError(f"非法 run 目录: {rd!r}")
    return p


def main(run_dir: str):
    run_dir = _safe_run_dir(run_dir)
    store = build_store()
    ms = store.us_rows  # noqa: F841  占位避免误删
    from engine.precompute import precompute
    from engine import rules as R
    pre = precompute(store, R.VERSIONS["C6"])
    ms_by_date = pre["ms"].set_index("date")["regime"].to_dict()

    rows = load_validation(run_dir)
    daily, eq, dd = build_curve(rows, mode="realtime")
    n_tr = sum(d["n_trade"] for d in daily)
    print(f"基线 T+1: 成交 {n_tr} 笔 费前累计 {(eq-1)*100:.1f}% 回撤 {dd*100:.1f}%")

    # 费后基线（build_curve 的 day_ret 为小数，费率 FEE% 直减 FEE/100）
    eq2, dd2, peak = 1.0, 0.0, 1.0
    for d in daily:
        day = d["day_ret"] - FEE / 100.0 * d["n_trade"] / max(d["n_pick"], 1)
        eq2 *= 1 + day
        peak = max(peak, eq2)
        dd2 = min(dd2, eq2 / peak - 1)
    print(f"基线 T+1: 费后({FEE}%) 累计 {(eq2-1)*100:.1f}% 回撤 {dd2*100:.1f}%")

    # ---- A. T+2 持有 ----
    f2 = t2_frame(run_dir, store)
    pnls1, pnls2 = [], []
    for r in f2.itertuples():
        p1 = trade_pnl({"group": r.group, "o": r.o, "h": r.h, "l": r.l, "c": r.c},
                       mode="realtime")
        p2 = pnl_t2({"group": r.group, "o": r.o, "h": r.h, "l": r.l, "c": r.c,
                     "t2_l": r.t2_l, "t2_c": r.t2_c})
        if p1 is not None and p2 is not None:
            pnls1.append(p1 * 100)
            pnls2.append(p2 * 100)
    a1, a2 = np.array(pnls1), np.array(pnls2)
    print(f"\nT+2 持有对照（同入场口径 n={len(a1)}，费前）:")
    print(f"  T+1 收盘: 均笔 {a1.mean():.3f}% 胜率 {(a1>0).mean()*100:.1f}%")
    print(f"  T+2 收盘: 均笔 {a2.mean():.3f}% 胜率 {(a2>0).mean()*100:.1f}%")
    print(f"  费后({FEE}%): T+1 {a1.mean()-FEE:.3f}% / T+2 {a2.mean()-FEE:.3f}%")

    # ---- B. 资金管理 ----
    print("\n资金管理层（费后日收益路径，含基准费后对照）:")
    # 重建费后 daily（day_ret 保持小数口径，每笔扣 FEE/100）
    daily_fee = []
    for d in daily:
        d2 = dict(d)
        d2["day_ret"] = d["day_ret"] - FEE / 100.0 * d["n_trade"] / max(d["n_pick"], 1)
        daily_fee.append(d2)
    for name, m in sizing_curves(daily_fee, ms_by_date).items():
        print(f"  {name}: 累计 {m['total']}% 回撤 {m['max_dd']}%")


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="C6/C7 持有期与资金管理实验")
    _ap.add_argument("--run", default="runs/us_round3_C6",
                     choices=["runs/us_round3_C6", "runs/us_round4_C7"],
                     help="实验对象 run 目录（静态白名单）")
    main(_ap.parse_args().run)
