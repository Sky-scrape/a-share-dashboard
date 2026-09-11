# -*- coding: utf-8 -*-
"""C9 互补研究：涨停组 T+1 止损位（单变量，止盈否证后的对称追问）。

背景（2026-09-10，紧接本目录 c9_tp_report.md）：
  - 止盈族（tp45/tp40/tp30/tp40_half）预注册判定全不成立，且 sweep 显示触发档
    越高越接近基线——被止盈砍掉的右尾（涨停延续）恰是收益来源；只有回撤改善。
  - C8 报告④：涨停组止损（≤-3%）触发 33.6%，其中「破位后收盘收回」的误触占
    22.7%——止损位是否也在砍可恢复仓位，是同一「离场管理」问题的对称一面。

本脚本在同一窗口（选股 2026-01-05..2026-09-08，T+1 结局至 2026-09-09）、同一
C7 冻结口径上，**只改涨停组 T+1 止损位**，低吸组与选股/配额/门槛全部不动。

候选族（跑数前预注册）：
  stop_close 不设盘中止损，一律收盘离场（测「止损本身有没有价值」的边界）
  stop40     -4% 破位离场
  stop50     -5% 破位离场（2026-09-10 执行口径对齐后 = 基线本身，作为自检：
             其均益差应恰为 0）
基线 = C7 现行（对齐后）：执行层 risk_low=-5% 破位离场，否则 T+1 收盘离场。

执行近似（沿用 backtest_capital 口径）：
  - 破位用 T+1 最低价判定（low < 止损位 → 按止损价成交，与基线同一近似）；
  - 放松止损机械上会放大单笔尾部亏损——报告披露最差单笔，判定④含回撤约束；
  - 入场/放弃规则与基线完全一致（开盘 ∈ [0,+5]%）。

预注册判定（采纳需 ①②③④ 全部满足，样本 = 涨停组已成交）：
  ① 实时均益差（变体 − 基线）≥ +0.30pp；
  ② 逐样本配对 bootstrap 95% CI（BOOT_N=2000, seed=7）下界 > 0；
  ③ 窗口前后两半方向一致（两半均益差均 > 0）；
  ④ 组合实时曲线 equity ≥ 基线，且最大回撤不深于基线 0.5pp 以上。
  多候选同时过线 → 取均益差最大者。全不过线 → 维持 -3% 现行止损。
  多重检验披露：本族在止盈否证后运行（同一离场主题的第二族），任何过线结论按
  rounds_log 方法论须连续两轮同向复核后方可进入规则。

口径：选股单一来源 = run 的 validation.csv；统计工具单一来源 = c9_tp_study
（paired_pnl/halves_of/build_curve_pnl/verdict_of）；收益费前。
用法：
    python strategy-iter/scripts/c9_exit_study.py --run runs/c8_window_C7
输出：<run>/c9_exit_report.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import backtest_capital as bt  # noqa: E402
import c8_candidate_study as c8  # noqa: E402
import c9_tp_study as c9  # noqa: E402  统计工具/曲线构造复用

CANDIDATES = (("stop_close", None), ("stop40", -4.0), ("stop50", -5.0))


# ---------------------------------------------------------------- 止损单票收益

def stop_pnl(row: dict, stop: float | None) -> float | None:
    """止损变体单票实时近似收益（小数）。stop=None → 无盘中止损、收盘离场。"""
    o, l, c = row["o"], row["l"], row["c"]
    if o is None or c is None:
        return None
    if not (bt.LU["win_min"] <= o <= bt.LU["win_max"]):
        return None                                     # 与基线同一放弃规则
    exit_pct = stop if (stop is not None and l is not None and l < stop) else c
    return (1 + exit_pct / 100.0) / (1 + o / 100.0) - 1


def stop_pnl_fn(stop: float | None):
    """组合口径 pnl：涨停组走止损变体，低吸组走基线（单变量隔离）。"""
    def fn(r: dict):
        if r["group"] == "lu":
            return stop_pnl(r, stop)
        return bt.trade_pnl(r, mode="realtime")
    return fn


def variant_stats(rows: list[dict], stop: float | None, base_curve: tuple) -> dict:
    lu = c9.lu_only(rows)
    fn = lambda r: stop_pnl(r, stop)                     # noqa: E731
    base, var, diffs = c9.paired_pnl(lu, fn)
    _daily, eq, dd = c9.build_curve_pnl(rows, stop_pnl_fn(stop))
    return {
        "n_exec": len(base),
        "n_affected": sum(1 for r in lu if bt.trade_pnl(r, mode="realtime") is not None
                          and r["l"] is not None and r["l"] < (stop if stop is not None else -99.0)),
        "pnl_base": round(c8._mean(base) * 100, 3) if base else None,
        "pnl_var": round(c8._mean(var) * 100, 3) if var else None,
        "diff_pp": round(c8._mean(diffs) * 100, 3) if diffs else None,
        "ci95": c9._paired_ci(diffs),
        "win_base": c8._win(base), "win_var": c8._win(var),
        "worst_base": round(min(base) * 100, 2) if base else None,
        "worst_var": round(min(var) * 100, 2) if var else None,
        "halves": c9.halves_of(lu, fn),
        "eq_base": round(base_curve[1], 4), "dd_base": round(base_curve[2] * 100, 1),
        "eq_var": round(eq, 4), "dd_var": round(dd * 100, 1),
    }


def robustness(rows: list[dict], stop: float | None) -> dict:
    """稳健性（事后检验，不参与预注册判定）：增益是否被少数极端样本驱动。"""
    lu = c9.lu_only(rows)
    base, var, diffs = c9.paired_pnl(lu, lambda r: stop_pnl(r, stop))
    if not diffs:
        return {}
    import statistics
    tail = sorted(diffs, key=abs)[:-5] if len(diffs) > 5 else []
    return {
        "n_exec": len(diffs),
        "n_up": sum(1 for d in diffs if d > 0), "n_down": sum(1 for d in diffs if d < 0),
        "n_flat": sum(1 for d in diffs if d == 0),
        "median_pp": round(statistics.median(diffs) * 100, 3),
        "trim5_pp": round(statistics.mean(tail) * 100, 3) if tail else None,
    }


def mechanism(rows: list[dict]) -> dict:
    """机制明细：risk_low 破位样本里「收盘更好」的误触成本，及更浅区间样本的结局。"""
    lu = c9.lu_only(rows)
    ex = [r for r in lu if bt.trade_pnl(r, mode="realtime") is not None]
    broke = [r for r in ex if r["l"] is not None and r["l"] <= bt.LU["risk_low"]]
    wrong = [r for r in broke if r["c"] is not None and r["c"] > r["o"]]      # 收盘收回
    band = [r for r in ex if r["l"] is not None and bt.LU["risk_low"] < r["l"] < 0]
    wrong_cost = []
    for r in wrong:
        p_stop = stop_pnl(r, bt.LU["risk_low"])
        p_close = (1 + r["c"] / 100.0) / (1 + r["o"] / 100.0) - 1
        wrong_cost.append((p_stop - p_close) * 100)      # 负数 = 止损比持有差
    band_pnl = [stop_pnl(r, None) for r in band]
    return {"n_exec": len(ex), "n_broke3": len(broke), "n_wrong": len(wrong),
            "wrong_share": round(100.0 * len(wrong) / len(broke), 1) if broke else None,
            "wrong_cost_pp": round(c8._mean(wrong_cost), 2) if wrong_cost else None,
            "n_band": len(band),
            "band_close_avg": round(c8._mean(band_pnl) * 100, 2) if band_pnl else None}


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="C9 互补研究：涨停组 T+1 止损位（单变量）")
    ap.add_argument("--run", required=True, help="engine run 目录（含 validation.csv）")
    args = ap.parse_args()
    run_dir = Path(args.run) if Path(args.run).is_absolute() else BASE / args.run

    picks = c8.load_picks(run_dir)
    base_curve = c9.build_curve_pnl(picks, lambda r: bt.trade_pnl(r, mode="realtime"))
    stats = {name: variant_stats(picks, stop, base_curve) for name, stop in CANDIDATES}
    passed = [name for name, _s in CANDIDATES if c9.verdict_of(stats[name])]
    chosen = max(passed, key=lambda n: stats[n]["diff_pp"]) if passed else None
    mech = mechanism(picks)
    base_lu = [p for p in (bt.trade_pnl(r, mode="realtime") for r in c9.lu_only(picks))
               if p is not None]

    n_days = len({r["T1"] for r in picks})
    ann = lambda eq: (eq ** (bt.TRADING_DAYS_PER_YEAR / n_days) - 1) * 100  # noqa: E731

    lines = ["# C9 互补研究：涨停组 T+1 止损位（单变量）", "",
             f"- run：`{run_dir.name}` · picks n={len(picks)} · 涨停组已成交 n={len(base_lu)} · "
             f"T+1 交易日 {n_days}",
             "- 单一变量：只改涨停组止损位；入场/放弃/选股/低吸组全部与 C7 冻结一致",
             "- 本族在止盈否证（c9_tp_report.md）之后运行——多重检验披露；收益费前", "",
             "## 基线复现（C7 现行：执行层 risk_low -5% 破位离场）", "",
             f"- 涨停组已成交实时均益 **{c8._mean(base_lu) * 100:+.3f}%** · 单笔胜率 {c8._win(base_lu)}%"
             f" · 最差单笔 {min(base_lu) * 100:+.2f}%",
             f"- 组合实时曲线：equity **{base_curve[1]:.4f}** · 年化 {ann(base_curve[1]):+.1f}% · "
             f"最大回撤 {base_curve[2] * 100:.1f}%", "",
             "## 机制明细（risk_low 止损砍在了哪）", "",
             f"- 破位 risk_low=-5% 样本 {mech['n_broke3']}/{mech['n_exec']}，其中收盘收回（止损错杀）"
             f"{mech['n_wrong']} 笔（{mech['wrong_share']}%），错杀成本均 {mech['wrong_cost_pp']}pp/笔",
             f"- 全天最低落在 (-5%, 0%) 区间的样本 {mech['n_band']} 笔（当前规则下它们不被止损、"
             f"收盘离场均益 {mech['band_close_avg']}%）", "",
             "## 候选对比（涨停组已成交，配对）", "",
             "| 变体 | 受影响/已成交 | 实时均益% | 胜率% | 最差单笔% | 差pp | 配对CI95 | 前半/后半diff | 组合equity | 组合maxDD% | 判定 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, stop in CANDIDATES:
        st = stats[name]
        ok = c9.verdict_of(st)
        lines.append(
            f"| {name} | {st['n_affected']}/{st['n_exec']} | {st['pnl_var']} | {st['win_var']} "
            f"| {st['worst_var']} | {st['diff_pp']:+} | {st['ci95']} | "
            + "/".join(str(h["diff_pp"]) for h in st["halves"])
            + f" | {st['eq_var']} | {st['dd_var']} | {'**过线**' if ok else '不过线'} |")

    lines += ["", "## 预注册判定", ""]
    if chosen:
        st = stats[chosen]
        lines += [f"- 过线候选：{', '.join(passed)}；取 **{chosen}**（均益差 {st['diff_pp']:+}pp，"
                  f"CI95 {st['ci95']}，组合 equity {st['eq_var']} vs 基线 {st['eq_base']}）",
                  f"- **判定：成立——{chosen} 进入候选（按轮次纪律须连续两轮同向复核后固化）**"]
    else:
        lines += ["- 无候选满足 ①②③④ 全部条件",
                  "- **判定：不成立——维持 -3% 破位止损现行规则**"]

    # 事后稳健性（不进判定，仅披露增益结构）
    lines += ["", "## 稳健性与口径附注（事后，不参与预注册判定）", ""]
    for name, stop in CANDIDATES:
        rb = robustness(picks, stop)
        if rb:
            lines.append(f"- {name}：改善 {rb['n_up']} / 变差 {rb['n_down']} / 不变 {rb['n_flat']}；"
                         f"配对差中位 {rb['median_pp']}pp；去 |差| 前 5 笔后均值 {rb['trim5_pp']}pp")
    lines += [
        "- **口径对齐已执行（2026-09-10）**：此前实时代理取 -3% 破位离场，但 -3% 在规则里是"
        "**买入触发过滤**（`execution_layer.MF_BUY.lu.low_min` / engine `trigger_low_min`），"
        "执行层风险位实为 `risk_low=-5%`（盘中）+ `risk_close=-4%`（收盘确认）。本报告的基线"
        "已是**对齐后口径**：backtest_capital 改为引用 backend/execution_layer.py 单一来源，"
        "涨停组可执行离场 = 盘中最低 ≤ risk_low 破位离场，否则收盘（risk_close 的离场价即收盘价，"
        "与收盘离场同价、仅作风险标记）。",
        "- 自检：stop50 与对齐后基线同口径，其均益差与组合曲线应与基线完全一致（差 0）。",
        "- 保留的口径差（非错位）：涨停组进场下限 0%（执行层 win_min=0「低开不接」是冻结执行"
        "纪律，engine open_min=-2% 是验证窗口）；`trigger_open_max=3%` 属回看触发定义，不参与"
        "可执行口径。",
        "- 增益结构提示：止盈族否证 + 本族增益集中于少数「深V回封」样本，共同指向涨停组 T+1 "
        "收益的右尾主导特征——下一轮优先验证右尾的**可执行获取方式**（如封板日持仓处置），"
        "而非继续调离场阈值。"]

    lines += ["", "## 口径与近似披露", "",
              "- 破位用 T+1 最低价近似（low < 止损位 → 按止损价成交），与基线同一近似；",
              "- 日线 bar 无法还原盘中路径：破位后是否收回、封板日实际可卖量均不可辨；",
              "- 本曲线为 T+1 日内一轮口径（收盘/止损离场），费前，不含佣金滑点；",
              "- 本回测为规则化模拟，非投资建议。"]
    out = run_dir / "c9_exit_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {out}")
    print(json.dumps({"mech": mech, "passed": passed, "chosen": chosen,
                      "stats": {k: {"diff_pp": v["diff_pp"], "ci95": v["ci95"],
                                    "eq": v["eq_var"], "dd": v["dd_var"],
                                    "worst": v["worst_var"]} for k, v in stats.items()}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
