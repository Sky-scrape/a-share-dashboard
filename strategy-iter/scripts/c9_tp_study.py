# -*- coding: utf-8 -*-
"""C9 候选研究：涨停组 T+1 盘中止盈机制（单变量，背景 = C8 报告④）。

背景（2026-09-10）：C8 候选验证④（runs/c8_window_C7/c8_candidates_report.md）显示
涨停组已成交样本 MFE 均值 +4.66% / 中位 +4.87%，而实时均益仅 +0.46%——浮盈在
T+1 收盘前大幅回吐。「无止盈机制」是当时列出的、下轮迭代最值得单变量检验的候选。

本脚本在 C7 冻结口径、与 C8 完全相同窗口（选股 2026-01-05..2026-09-08，T+1 结局至
2026-09-09）上，**只改动涨停组的 T+1 离场规则**，低吸组（nlu）与选股/配额/门槛全部
不动，做单变量对照。

候选族（跑数前预注册，参数锚定④的 MFE 分布，防止挑参数）：
  tp45      触及入场价 +4.5% 全仓离场   （MFE 中位数 +4.87% 取整锚定）
  tp40      触及 +4.0% 全仓离场
  tp30      触及 +3.0% 全仓离场
  tp40_half +4.0% 触发半仓离场、另一半持有至收盘
基线 = C7 现行规则（2026-09-10 执行口径对齐后）：执行层风险位 risk_low=-5% 破位离场，
否则 T+1 收盘离场。

执行近似（日线 bar，沿用 backtest_capital 口径并新增止盈分支，报告原样披露）：
  - 触价判定用 T+1 最高价：high 相对入场的最大可达增益 ≥ 触发值 → 假设按触发价
    成交（与低吸组「最高价触及挂单价」同一近似，不用收盘信息做触发决策）；
  - 止损与止盈同日皆可触发时，盘中先后日线不可辨 → 按止损优先（保守，对止盈候选
    不利；基线同口径，比较公平）；
  - 涨跌停自锁由实际 high 判定：一字板/封板不可达的触发值自然不触发；
  - 入场与放弃规则与基线完全一致（开盘 ∈ [0,+5]% 市价买入，低开/高开>+5% 放弃）。

预注册判定（采纳为 C9 候选规则需 ①②③④ 全部满足，样本 = 涨停组已成交）：
  ① 实时均益差（变体 − 基线）≥ +0.30pp；
  ② 逐样本配对 bootstrap 95% CI（BOOT_N=2000, seed=7）下界 > 0；
  ③ 窗口前后两半方向一致（两半的均益差均 > 0）；
  ④ 组合实时曲线（涨停组变体 + 低吸组现行）equity ≥ 基线，且最大回撤不深于基线
     0.5pp 以上。
  多个候选同时过线 → 取均益差最大者（并列取单笔胜率高者）。全不过线 → 不进系统，
  维持「T+1 收盘离场」铁律。2.0~6.0 全档 sweep 仅作敏感性披露，不参与选择。

口径：选股单一来源 = run 的 validation.csv（本脚本不重新选股）；执行层常量单一来源
= backtest_capital.bt.LU / bt.NLU；收益费前（与 C8 各实验同一口径）。
用法：
    python strategy-iter/scripts/c9_tp_study.py --run runs/c8_window_C7
输出：<run>/c9_tp_report.md；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import backtest_capital as bt  # noqa: E402  执行口径单一来源
import c8_candidate_study as c8  # noqa: E402  统计工具/装载复用（main 有守卫）

BOOT_N = 2000
BOOT_SEED = 7
CANDIDATES = (("tp45", 4.5, False), ("tp40", 4.0, False),
              ("tp30", 3.0, False), ("tp40_half", 4.0, True))
SWEEP = [round(2.0 + 0.5 * i, 1) for i in range(9)]   # 2.0..6.0，仅披露


# ---------------------------------------------------------------- 止盈单票收益

def tp_fired(row: dict, trigger: float) -> bool:
    """T+1 盘中是否触及入场价 +trigger%（止损优先日内不辨，由 tp_pnl 处理）。"""
    o, h = row["o"], row["h"]
    if o is None or h is None:
        return False
    return ((1 + h / 100.0) / (1 + o / 100.0) - 1) * 100.0 >= trigger


def tp_pnl(row: dict, trigger: float, half: bool = False) -> float | None:
    """止盈变体单票实时近似收益（小数）。不成交/数据缺失返回 None；未触发时
    与基线（backtest_capital.trade_pnl realtime，执行层风险位 risk_low=-5%）逐字一致：
    破位即离场，否则收盘。"""
    o, h, l, c = row["o"], row["h"], row["l"], row["c"]
    if o is None or c is None:
        return None
    if not (bt.LU["win_min"] <= o <= bt.LU["win_max"]):
        return None                                     # 与基线同一放弃规则
    if l is not None and l <= bt.LU["risk_low"]:
        return (1 + bt.LU["risk_low"] / 100.0) / (1 + o / 100.0) - 1   # 风险位优先（保守）
    if tp_fired(row, trigger):
        if half:
            return 0.5 * (trigger / 100.0) + 0.5 * ((1 + c / 100.0) / (1 + o / 100.0) - 1)
        return trigger / 100.0                          # 按触发价成交（费前）
    return (1 + c / 100.0) / (1 + o / 100.0) - 1        # 未触发 = 基线收盘离场


def lu_only(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["group"] == "lu"]


def paired_pnl(rows: list[dict], pnl_fn):
    """(基线 pnl, 变体 pnl, 逐样本差) 配对列表；成交集由基线是否 None 决定。

    仅离场规则改变时两侧成交集恒等（入场/放弃规则未动），差值可直接配对。
    """
    base, var = [], []
    for r in rows:
        b = bt.trade_pnl(r, mode="realtime")
        if b is None:
            continue
        base.append(b)
        var.append(pnl_fn(r))
    return base, var, [v - b for b, v in zip(base, var)]


def halves_of(rows: list[dict], pnl_fn):
    """按 T 中位数切前后半窗的均益差（pp），判定③用。"""
    ts = sorted({r["T"] for r in rows})
    mid = ts[len(ts) // 2]
    out = []
    for lo, hi, tag in ((None, mid, "h1"), (mid, None, "h2")):
        sel = [r for r in rows if (lo is None or r["T"] >= lo) and (hi is None or r["T"] < hi)]
        base, var, _d = paired_pnl(sel, pnl_fn)
        out.append({"half": tag, "n": len(base),
                    "diff_pp": round((c8._mean(var) - c8._mean(base)) * 100, 2)
                    if base and var else None})
    return out


def _paired(rows: list[dict], trigger: float, half: bool):
    """涨停组止盈 (基线, 变体, 差) 配对列表（止盈专用薄壳）。"""
    return paired_pnl(rows, lambda r: tp_pnl(r, trigger, half))


def _paired_ci(diffs: list[float], n: int = BOOT_N, seed: int = BOOT_SEED):
    """配对均值的百分位 bootstrap 95% CI（pp）。样本不足返回 None。"""
    import numpy as np
    if len(diffs) < 5:
        return None
    a = np.asarray(diffs)
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), (n, len(a)))].mean(axis=1)
    return (round(float(np.percentile(means, 2.5)) * 100, 2),
            round(float(np.percentile(means, 97.5)) * 100, 2))


def build_curve_pnl(rows: list[dict], pnl_fn):
    """按 T+1 日聚合等权组合（结构同 bt.build_curve，pnl 由 pnl_fn 提供）。"""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["T1"], []).append(r)
    daily, eq, peak, max_dd = [], 1.0, 1.0, 0.0
    for t1 in sorted(by_day):
        pnls = []
        for r in sorted(by_day[t1], key=lambda x: x["thscode"]):
            p = pnl_fn(r)
            if p is not None:
                pnls.append(p)
        day_ret = sum(pnls) / len(pnls) if pnls else 0.0
        eq *= 1 + day_ret
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
        daily.append({"T1": t1, "n_trade": len(pnls), "day_ret": day_ret, "equity": eq})
    return daily, eq, max_dd


def variant_pnl(trigger: float, half: bool):
    """组合口径 pnl：涨停组走止盈变体，低吸组走基线（单变量隔离）。"""
    def fn(r: dict):
        if r["group"] == "lu":
            return tp_pnl(r, trigger, half)
        return bt.trade_pnl(r, mode="realtime")
    return fn


# ---------------------------------------------------------------- 统计

def half_diffs(rows: list[dict], trigger: float, half: bool):
    """按 T 中位数切前后半窗的均益差（pp），判定③用（止盈专用薄壳）。"""
    return halves_of(rows, lambda r: tp_pnl(r, trigger, half))


def variant_stats(rows: list[dict], trigger: float, half: bool, base_curve: tuple) -> dict:
    """单变量统计：LU 已成交均益/胜率/触发率/差/CI/半窗 + 组合曲线对照。"""
    lu = lu_only(rows)
    base, var, diffs = _paired(lu, trigger, half)
    fired = [r for r in lu if bt.trade_pnl(r, mode="realtime") is not None
             and tp_fired(r, trigger) and not (r["l"] is not None and r["l"] < bt.LU["low_min"])]
    _daily, eq, dd = build_curve_pnl(rows, variant_pnl(trigger, half))
    return {
        "n_exec": len(base),
        "n_fired": len(fired),
        "fire_rate": round(100.0 * len(fired) / len(base), 1) if base else None,
        "pnl_base": round(c8._mean(base) * 100, 3) if base else None,
        "pnl_var": round(c8._mean(var) * 100, 3) if var else None,
        "diff_pp": round(c8._mean(diffs) * 100, 3) if diffs else None,
        "ci95": _paired_ci(diffs),
        "win_base": c8._win(base), "win_var": c8._win(var),
        "halves": half_diffs(lu, trigger, half),
        "eq_base": round(base_curve[1], 4), "dd_base": round(base_curve[2] * 100, 1),
        "eq_var": round(eq, 4), "dd_var": round(dd * 100, 1),
    }


def verdict_of(st: dict) -> bool:
    """预注册判定①②③④，全部满足才通过。"""
    if st["diff_pp"] is None or st["ci95"] is None:
        return False
    c1 = st["diff_pp"] >= 0.30
    c2 = st["ci95"][0] > 0
    c3 = all(h["diff_pp"] is not None and h["diff_pp"] > 0 for h in st["halves"])
    c4 = st["eq_var"] >= st["eq_base"] and st["dd_var"] >= st["dd_base"] - 0.5
    return c1 and c2 and c3 and c4


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="C9 候选研究：涨停组 T+1 盘中止盈（单变量）")
    ap.add_argument("--run", required=True, help="engine run 目录（含 validation.csv）")
    args = ap.parse_args()
    run_dir = Path(args.run) if Path(args.run).is_absolute() else BASE / args.run

    picks = c8.load_picks(run_dir)
    lu = lu_only(picks)
    base_curve = build_curve_pnl(picks, lambda r: bt.trade_pnl(r, mode="realtime"))
    base_lu = [p for p in (bt.trade_pnl(r, mode="realtime") for r in lu) if p is not None]

    stats = {name: variant_stats(picks, trig, half, base_curve)
             for name, trig, half in CANDIDATES}
    passed = [name for name, _t, _h in CANDIDATES if verdict_of(stats[name])]
    chosen = None
    if passed:
        chosen = max(passed, key=lambda n: (stats[n]["diff_pp"], stats[n]["win_var"] or 0))

    sweep = []
    for trig in SWEEP:
        base, var, diffs = _paired(lu, trig, False)
        _d, eq, dd = build_curve_pnl(picks, variant_pnl(trig, False))
        sweep.append({"trigger": trig, "n_fired": sum(
            1 for r in lu if bt.trade_pnl(r, mode="realtime") is not None
            and tp_fired(r, trig) and not (r["l"] is not None and r["l"] < bt.LU["low_min"])),
            "pnl_avg": round(c8._mean(var) * 100, 3) if var else None,
            "diff_pp": round(c8._mean(diffs) * 100, 3) if diffs else None,
            "eq": round(eq, 4), "dd": round(dd * 100, 1)})

    n_days = len({r["T1"] for r in picks})
    ann = lambda eq: (eq ** (bt.TRADING_DAYS_PER_YEAR / n_days) - 1) * 100  # noqa: E731

    lines = ["# C9 候选研究：涨停组 T+1 盘中止盈（单变量）", "",
             f"- run：`{run_dir.name}` · picks n={len(picks)} · 涨停组 n={len(lu)} · "
             f"涨停组已成交 n={len(base_lu)} · T+1 交易日 {n_days}",
             "- 单一变量：只改涨停组 T+1 离场规则；选股/配额/门槛/环境/低吸组全部与 C7 冻结一致",
             "- 收益费前；执行近似见文末披露；判定规则跑数前预注册（见脚本头）", "",
             "## 基线复现（C7 现行：执行层 risk_low -5% 破位离场，否则收盘离场）", "",
             f"- 涨停组已成交实时均益 **{c8._mean(base_lu) * 100:+.3f}%** · 单笔胜率 {c8._win(base_lu)}%",
             f"- 组合实时曲线：equity **{base_curve[1]:.4f}** · 年化 {ann(base_curve[1]):+.1f}% · "
             f"最大回撤 {base_curve[2] * 100:.1f}%", "",
             "## 候选对比（涨停组已成交，配对）", "",
             "| 变体 | 触发数/已成交 | 触发率% | 实时均益% | 胜率% | 差pp | 配对CI95 | 前半/后半diff | 组合equity | 组合maxDD% | 判定 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, trig, half in CANDIDATES:
        st = stats[name]
        ok = verdict_of(st)
        lines.append(
            f"| {name} | {st['n_fired']}/{st['n_exec']} | {st['fire_rate']} | {st['pnl_var']} "
            f"| {st['win_var']} | {st['diff_pp']:+} | {st['ci95']} | "
            + "/".join(str(h["diff_pp"]) for h in st["halves"])
            + f" | {st['eq_var']} | {st['dd_var']} | {'**过线**' if ok else '不过线'} |")

    lines += ["", "## 触发档 sweep（2.0~6.0，仅敏感性披露、不参与选择）", "",
              "| 触发% | 触发数 | 实时均益% | 差pp | 组合equity | 组合maxDD% |", "|---|---|---|---|---|---|"]
    for s in sweep:
        lines.append(f"| {s['trigger']} | {s['n_fired']} | {s['pnl_avg']} | {s['diff_pp']:+} | {s['eq']} | {s['dd']} |")

    lines += ["", "## 预注册判定", ""]
    if chosen:
        st = stats[chosen]
        trig = dict((n, t) for n, t, _h in CANDIDATES)[chosen]
        lines += [f"- 过线候选：{', '.join(passed)}；按预注册选择规则取 **{chosen}**（触发 {trig}%，"
                  f"均益差 {st['diff_pp']:+}pp，CI95 {st['ci95']}，组合 equity {st['eq_var']} vs 基线 {st['eq_base']}）",
                  f"- **判定：成立——{chosen} 进入 C9 候选规则（待按轮次纪律复核后固化）**"]
    else:
        lines += ["- 无候选满足 ①②③④ 全部条件",
                  "- **判定：不成立——维持「T+1 收盘离场」现行规则，止盈候选不进策略系统**"]

    lines += ["", "## 口径与近似披露（原样，同 backtest_capital 头注）", "",
              "- 触价成交用 T+1 最高价近似（high 触及触发等价位即按触发价成交），与低吸组"
              "「最高价触及挂单价」同一近似；不用收盘信息做触发决策。",
              "- 止损与止盈同日皆可触发时按**止损优先**（保守，系统性低估止盈候选收益）。",
              "- 日线 bar 无法还原盘中路径：触发前是否先破位、封板日实际可卖量均不可辨；"
              "本曲线为 T+1 日内一轮口径（收盘/止损/止盈离场），费前，不含佣金滑点。",
              "- 本回测为规则化模拟，非投资建议。"]
    out = run_dir / "c9_tp_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {out}")
    print(json.dumps({"baseline_lu_pnl": round(c8._mean(base_lu) * 100, 3),
                      "passed": passed, "chosen": chosen,
                      "stats": {k: {"diff_pp": v["diff_pp"], "ci95": v["ci95"],
                                    "eq": v["eq_var"], "dd": v["dd_var"]} for k, v in stats.items()},
                      "sweep": sweep}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
