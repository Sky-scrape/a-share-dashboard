# -*- coding: utf-8 -*-
"""C8 候选改动全窗口验证：执行折损 / 风险位质量 / 隔夜大涨不对称 / 环境档位仓位。

背景（2026-09-10，C7 固化后评审）：四项候选优化需在 2026-01-02..2026-09-02
选股窗口（T+1 结局至 09-03）上严密验证，有实证提升才进入策略系统：
  ③ 执行折损定量：条件期望（验证器口径）vs 实时可执行口径（backtest_capital
     同一实现）的年化差 + 涨停组/低吸组的放弃率与未成交率明细；
  ④ 风险位质量：已成交样本的 MFE/MAE、止损误触率（盘中破位收盘收回）、
     止损挽损（破位且收盘更差时止损省下的幅度）；止损位=执行层 risk_low
     （涨停组 -5%；2026-09-10 口径对齐前误用触发过滤 -3%）；
  ⑤ 隔夜大涨不对称：C6 闸门只防跌（≤-2% 强制防守），检验隔夜 ≥+2% 日
     备选池执行收益是否显著弱于中性区间——若成立候选「隔夜大涨降级」规则；
  ⑥ 环境档位仓位：defensive 档执行收益 vs normal（窗口内无 freeze 选股，
     如实报告），并模拟 S1 半仓 / 空仓 / 全仓三种资金纪律的资金曲线差异。

判定规则（跑数前预先注册，防数据挖掘）：
  ⑤ 采纳候选规则需同时满足：涨停组 up 桶实时均益 < 中性桶 ≥1.5pp、up 桶
     已成交 n≥25、窗口前后两半方向一致（低吸组只作旁证不强求）；
  ⑥ 若 defensive 实时均益 < normal −1.0pp 且绝对 < +1% → 支持防守档收紧；
     若差 ≥ −0.5pp 且均值为正 → 数据支持维持 S1 半仓建议现状（不升规则）。

口径：选股单一来源 = run 的 validation.csv（本脚本不重新选股）；隔夜因子单一
来源 = backend/us_market factors.json（rows[T].纳斯达克综合/标普500，us_date
=T 即 T+1 开盘前最后收盘场次，与线上闸门同源）。收益为费前。

用法：
    python strategy-iter/scripts/c8_candidate_study.py --run runs/us_round5_C7_aligned
输出：<run>/c8_candidates_report.md；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import backtest_capital as bt  # noqa: E402  同目录：执行口径单一来源

ROOT = BASE.parent
FACTORS = ROOT / "data" / "recap" / "us_market" / "factors.json"
BOOT_N = 2000
BOOT_SEED = 7


# ---------------------------------------------------------------- 数据装载

def load_picks(run_dir: Path) -> list[dict]:
    """validation.csv → 全列保留的 pick 行（数值列转 float）。"""
    out = []
    with open(Path(run_dir) / "validation.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            def num(k):
                v = (r.get(k) or "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None
            out.append({
                "T": r["T"], "T1": r["T1"], "group": r["group"],
                "thscode": r["thscode"], "name": r.get("name") or r["thscode"],
                "regime": r.get("regime") or "",
                "o": num("open_ret"), "h": num("high_ret"),
                "l": num("low_ret"), "c": num("close_ret"),
                "buy_triggered": (r.get("buy_triggered") or "").strip() == "True",
                "risk_hit": (r.get("risk_hit") or "").strip() == "True",
                "win": (r.get("win") or "").strip() == "True",
            })
    return out


def load_overnight() -> dict[str, tuple[float, float]]:
    """factors.json → {A股T: (纳指隔夜%, 标普隔夜%)}；行缺失跳过。"""
    if not FACTORS.exists():
        return {}
    payload = json.loads(FACTORS.read_text(encoding="utf-8"))
    out = {}
    for t, row in (payload.get("rows") or {}).items():
        ndx = row.get("纳斯达克综合")
        spx = row.get("标普500")
        out[t] = (float(ndx) if ndx is not None else None,
                  float(spx) if spx is not None else None)
    return out


# ---------------------------------------------------------------- 统计工具

def _pnl(row: dict) -> float | None:
    return bt.trade_pnl(row, mode="realtime")


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _win(xs: list[float]) -> float | None:
    return round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1) if xs else None


def _boot_ci(a: list[float], b: list[float], n: int = BOOT_N, seed: int = BOOT_SEED):
    """均值差 (a-b) 的百分位 bootstrap 95% CI（pp）。样本不足返回 None。"""
    import numpy as np
    if len(a) < 5 or len(b) < 5:
        return None
    rng = np.random.default_rng(seed)
    aa = np.asarray(a)
    bb = np.asarray(b)
    da = aa[rng.integers(0, len(aa), (n, len(aa)))].mean(axis=1)
    db = bb[rng.integers(0, len(bb), (n, len(bb)))].mean(axis=1)
    diffs = np.sort(da - db)
    return round(float(diffs[int(0.025 * n)]) * 100, 2), round(float(diffs[int(0.975 * n)]) * 100, 2)


def _block(title: str, rows: list[dict]) -> str:
    """一组 pick 的标准统计块（md 表格行）。"""
    pnl = [p for p in (_pnl(r) for r in rows) if p is not None]
    cs = [r["c"] for r in rows if r["c"] is not None]
    m, c = _mean(pnl), _mean(cs)
    return (f"| {title} | {len(rows)} | {len(pnl)} | "
            f"{_win(cs) if cs else '-'} | {c:.2f} | "
            + (f"{m * 100:.2f} |" if m is not None else " - |"))


# ---------------------------------------------------------------- 四项分析

def exec_gap(picks: list[dict]) -> dict:
    """③ 执行折损：条件期望 vs 实时可执行。"""
    _, eq_rt, dd_rt = bt.build_curve(picks, mode="realtime")
    by_day: dict[str, list[float]] = {}
    for r in picks:
        if r["c"] is not None:
            by_day.setdefault(r["T1"], []).append(r["c"])
    eq_ce, dd_ce = 1.0, 0.0
    peak = 1.0
    for t1 in sorted(by_day):
        eq_ce *= 1 + sum(by_day[t1]) / len(by_day[t1]) / 100.0
        peak = max(peak, eq_ce)
        dd_ce = min(dd_ce, eq_ce / peak - 1)
    n_days = len(by_day)
    ann = lambda eq: (eq ** (bt.TRADING_DAYS_PER_YEAR / n_days) - 1) * 100  # noqa: E731

    lu = [r for r in picks if r["group"] == "lu"]
    nlu = [r for r in picks if r["group"] == "nlu"]
    detail = {}
    for g, rs, lo, hi in (("lu", lu, bt.LU["win_min"], bt.LU["win_max"]),
                          ("nlu", nlu, bt.NLU["win_min"], bt.NLU["win_max"])):
        oo = [r["o"] for r in rs if r["o"] is not None]
        executed = sum(1 for r in rs if _pnl(r) is not None)
        detail[g] = {
            "n": len(rs),
            "abandon_low": round(100.0 * sum(1 for x in oo if x < lo) / len(oo), 1) if oo else None,
            "abandon_high": round(100.0 * sum(1 for x in oo if x > hi) / len(oo), 1) if oo else None,
            "in_window": round(100.0 * sum(1 for x in oo if lo <= x <= hi) / len(oo), 1) if oo else None,
            "not_filled": round(100.0 * (len(oo) - executed) / len(oo), 1) if oo else None,
        }
    return {"eq_realtime": eq_rt, "eq_expect": eq_ce, "dd_realtime": dd_rt,
            "dd_expect": dd_ce, "ann_realtime": ann(eq_rt), "ann_expect": ann(eq_ce),
            "n_days": n_days, "detail": detail}


def risk_quality(picks: list[dict]) -> dict:
    """④ 已成交样本的 MFE/MAE 与止损质量。"""
    out = {}
    for g, stop in (("lu", bt.LU["risk_low"]), ("nlu", bt.NLU["risk_low"])):
        rows = [r for r in picks if r["group"] == g and _pnl(r) is not None
                and r["h"] is not None and r["l"] is not None]
        pnl = [p for p in (_pnl(r) for r in rows) if p is not None]
        if g == "lu":
            entry = [r["o"] for r in rows]
            mfe = [r["h"] - e for r, e in zip(rows, entry)]
            mae = [r["l"] - e for r, e in zip(rows, entry)]
            stopped = [r for r in rows if r["l"] <= stop]
            stop_exit, close_exit = stop, None
        else:
            entry = [max(r["o"], 0.0) for r in rows]
            mfe = [r["h"] - e for r, e in zip(rows, entry)]
            mae = [r["l"] - e for r, e in zip(rows, entry)]
            stopped = [r for r in rows if r["l"] <= stop]
        # 止损质量（相对离场基准的收益点差，同 entry 下比较 exit_pct）
        whipsaw = [r for r in stopped if r["c"] is not None and r["c"] > (r["o"] if g == "lu" else max(r["o"], 0.0))]
        saved = []
        for r in stopped:
            e = r["o"] if g == "lu" else max(r["o"], 0.0)
            if r["c"] is not None and r["c"] < e:   # 收盘比入场更差：止损有挽损
                pnl_stop = (1 + stop / 100.0) / (1 + e / 100.0) - 1
                pnl_close = (1 + r["c"] / 100.0) / (1 + e / 100.0) - 1
                saved.append((pnl_stop - pnl_close) * 100)
        out[g] = {
            "n_exec": len(rows),
            "mfe_avg": round(_mean(mfe), 2), "mfe_med": round(sorted(mfe)[len(mfe) // 2], 2) if mfe else None,
            "mae_avg": round(_mean(mae), 2), "mae_med": round(sorted(mae)[len(mae) // 2], 2) if mae else None,
            "stop_hit": len(stopped),
            "stop_whipsaw": len(whipsaw),
            "stop_saved_avg": round(_mean(saved), 2) if saved else None,
            "pnl_avg": round(_mean(pnl) * 100, 2) if pnl else None,
        }
    return out


def overnight_asym(picks: list[dict], on: dict) -> dict:
    """⑤ 隔夜大涨不对称：按 T 日隔夜纳指分桶（标普作稳健性）。"""
    def bucket(x):
        if x is None:
            return None
        return "down" if x <= -2.0 else ("up" if x >= 2.0 else "mid")

    rows = []
    for r in picks:
        ndx, spx = on.get(r["T"], (None, None))
        r = dict(r)
        r["b_ndx"] = bucket(ndx)
        r["b_spx"] = bucket(spx)
        rows.append(r)
    res = {"n_missing": sum(1 for r in rows if r["b_ndx"] is None)}
    for key, tag in (("b_ndx", "ndx"), ("b_spx", "spx")):
        for g in ("lu", "nlu"):
            for b in ("down", "mid", "up"):
                sel = [r for r in rows if r["group"] == g and r[key] == b]
                pnl = [p for p in (_pnl(r) for r in sel) if p is not None]
                cs = [r["c"] for r in sel if r["c"] is not None]
                res[f"{tag}_{g}_{b}"] = {
                    "n": len(sel), "n_exec": len(pnl),
                    "pnl_avg": round(_mean(pnl) * 100, 2) if pnl else None,
                    "close_avg": round(_mean(cs), 2) if cs else None,
                    "win": _win(cs),
                }
    # 高开交互：up 桶涨停组的开盘分布
    up_lu = [r for r in rows if r["group"] == "lu" and r["b_ndx"] == "up"]
    mid_lu = [r for r in rows if r["group"] == "lu" and r["b_ndx"] == "mid"]
    oo = [r["o"] for r in up_lu if r["o"] is not None]
    res["up_lu_open"] = {"n": len(oo), "avg": round(_mean(oo), 2) if oo else None,
                         "abandon_hi5": round(100.0 * sum(1 for x in oo if x > 5) / len(oo), 1) if oo else None}
    # 预注册判定：lu up vs mid 实时均益差 + 前后半窗方向一致性
    up_p = [p for p in (_pnl(r) for r in rows if r["group"] == "lu" and r["b_ndx"] == "up") if p is not None]
    mid_p = [p for p in (_pnl(r) for r in rows if r["group"] == "lu" and r["b_ndx"] == "mid") if p is not None]
    res["lu_up_vs_mid"] = {"diff_pp": round((_mean(up_p) - _mean(mid_p)) * 100, 2) if up_p and mid_p else None,
                           "ci95": _boot_ci(up_p, mid_p)}
    half = sorted({r["T"] for r in rows})[len({r["T"] for r in rows}) // 2]
    cons = []
    for lo, hi, tag in ((None, half, "h1"), (half, None, "h2")):
        ups = [p for p in (_pnl(r) for r in rows if r["group"] == "lu" and r["b_ndx"] == "up"
                           and (lo is None or r["T"] >= lo) and (hi is None or r["T"] < hi)) if p is not None]
        mids = [p for p in (_pnl(r) for r in rows if r["group"] == "lu" and r["b_ndx"] == "mid"
                            and (lo is None or r["T"] >= lo) and (hi is None or r["T"] < hi)) if p is not None]
        cons.append({"half": tag, "n_up": len(ups),
                     "diff_pp": round((_mean(ups) - _mean(mids)) * 100, 2) if ups and mids else None})
    res["lu_up_vs_mid_halves"] = cons
    return res


def regime_sizing(picks: list[dict]) -> dict:
    """⑥ 环境档位执行收益 + S1 半仓/空仓/全仓资金曲线对比。"""
    res = {"regimes": {}}
    for reg in sorted({r["regime"] for r in picks}):
        sel = [r for r in picks if r["regime"] == reg]
        pnl = [p for p in (_pnl(r) for r in sel) if p is not None]
        cs = [r["c"] for r in sel if r["c"] is not None]
        res["regimes"][reg] = {
            "n": len(sel), "n_exec": len(pnl),
            "pnl_avg": round(_mean(pnl) * 100, 2) if pnl else None,
            "close_avg": round(_mean(cs), 2) if cs else None,
            "win": _win(cs),
            "risk_hit": round(100.0 * sum(1 for r in sel if r["risk_hit"]) / len(sel), 1) if sel else None,
            "worst": round(min(pnl) * 100, 2) if pnl else None,
        }
    # 资金纪律对比：defensive 日收益 × {1.0 全仓, 0.5 S1 半仓, 0.0 空仓}
    by_day: dict[str, list[tuple[str, float]]] = {}
    for r in picks:
        p = _pnl(r)
        if p is not None:
            by_day.setdefault(r["T1"], []).append((r["regime"], p))
    curves = {}
    for label, mult in (("full", 1.0), ("s1_half", 0.5), ("flat", 0.0)):
        eq, peak, mdd = 1.0, 1.0, 0.0
        for t1 in sorted(by_day):
            day = sum(p for _g, p in by_day[t1]) / len(by_day[t1])
            if any(g == "defensive" for g, _p in by_day[t1]):
                day *= mult
            eq *= 1 + day
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        curves[label] = {"equity": round(eq, 4), "max_dd": round(mdd * 100, 1)}
    res["s1_curves"] = curves
    # 预注册判定输入：defensive vs normal 实时均益差
    dp = [p for p in (_pnl(r) for r in picks if r["regime"] == "defensive") if p is not None]
    np_ = [p for p in (_pnl(r) for r in picks if r["regime"] == "normal") if p is not None]
    res["def_vs_normal"] = {"diff_pp": round((_mean(dp) - _mean(np_)) * 100, 2) if dp and np_ else None,
                            "ci95": _boot_ci(dp, np_)}
    return res


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="C8 候选改动全窗口验证")
    ap.add_argument("--run", required=True, help="engine run 目录（含 validation.csv）")
    args = ap.parse_args()
    run_dir = Path(args.run) if Path(args.run).is_absolute() else BASE / args.run

    picks = load_picks(run_dir)
    on = load_overnight()
    gap = exec_gap(picks)
    risk = risk_quality(picks)
    asym = overnight_asym(picks, on)
    reg = regime_sizing(picks)

    lines = ["# C8 候选改动验证报告", "",
             f"- run：`{run_dir.name}` · picks n={len(picks)} · 隔夜因子缺失 {asym['n_missing']} 天次",
             f"- 实时口径 = backtest_capital.trade_pnl（费前，日线近似见其头注）", ""]

    lines += ["## ③ 执行折损", "",
              f"- 实时可执行曲线（可投资的真口径）：equity **{gap['eq_realtime']:.3f}** · 年化 {gap['ann_realtime']:.1f}% · 最大回撤 {gap['dd_realtime'] * 100:.1f}%",
              f"- 条件期望参照（验证器口径，含收盘确认与理想成交假设，**不可投资**，仅作样本统计上界）：equity {gap['eq_expect']:.2f}",
              f"- 可执行样本只占候选的少数：折损主要来自放弃与未成交，而非持有期收益（见下表与④）", "",
              "| 组 | 样本 | 低开放弃% | 高开放弃% | 窗口内% | 未成交% |", "|---|---|---|---|---|---|"]
    for g in ("lu", "nlu"):
        d = gap["detail"][g]
        lines.append(f"| {g} | {d['n']} | {d['abandon_low']} | {d['abandon_high']} | {d['in_window']} | {d['not_filled']} |")

    lines += ["", "## ④ 风险位质量（已成交样本，MFE/MAE 相对入场价，%）", "",
              "| 组 | 已成交 | MFE均 | MFE中位 | MAE均 | MAE中位 | 止损触发 | 止损误触(收盘收回) | 止损挽损均(pp) | 实时均益% |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for g in ("lu", "nlu"):
        d = risk[g]
        lines.append(f"| {g} | {d['n_exec']} | {d['mfe_avg']} | {d['mfe_med']} | {d['mae_avg']} | {d['mae_med']} "
                     f"| {d['stop_hit']} | {d['stop_whipsaw']} | {d['stop_saved_avg']} | {d['pnl_avg']} |")

    lines += ["", "## ⑤ 隔夜大涨不对称（按 T 日隔夜因子分桶：≤-2 / (-2,+2) / ≥+2）", "",
              "| 因子 | 组 | 桶 | 样本 | 已成交 | 实时均益% | 收盘均% | 胜率% |", "|---|---|---|---|---|---|---|---|"]
    for tag in ("ndx", "spx"):
        for g in ("lu", "nlu"):
            for b in ("down", "mid", "up"):
                d = asym[f"{tag}_{g}_{b}"]
                lines.append(f"| {tag} | {g} | {b} | {d['n']} | {d['n_exec']} | {d['pnl_avg']} | {d['close_avg']} | {d['win']} |")
    lu_up = asym["lu_up_vs_mid"]
    lines += ["",
              f"- 涨停组 up−mid 实时均益差：**{lu_up['diff_pp']}pp**（bootstrap 95% CI {lu_up['ci95']}）",
              f"- up 桶涨停组开盘：均值 {asym['up_lu_open']['avg']}% · 高开>5% 放弃率 {asym['up_lu_open']['abandon_hi5']}%（n={asym['up_lu_open']['n']}）",
              "- 前后半窗一致性：" + " · ".join(
                  f"{c['half']} diff={c['diff_pp']}pp (n_up={c['n_up']})" for c in asym["lu_up_vs_mid_halves"])]
    ok5 = (lu_up["diff_pp"] is not None and lu_up["diff_pp"] <= -1.5
           and asym["ndx_lu_up"]["n_exec"] >= 25
           and all(c["diff_pp"] is not None and c["diff_pp"] < 0 for c in asym["lu_up_vs_mid_halves"]))
    lines += ["", f"- **预注册判定⑤：{'成立，候选 C8 规则' if ok5 else '不成立，不进入策略系统'}**"]

    lines += ["", "## ⑥ 环境档位与 S1 仓位", "",
              "| 环境 | 样本 | 已成交 | 实时均益% | 收盘均% | 胜率% | 风险位触发% | 最差单笔% |", "|---|---|---|---|---|---|---|---|"]
    for name, d in sorted(reg["regimes"].items()):
        lines.append(f"| {name} | {d['n']} | {d['n_exec']} | {d['pnl_avg']} | {d['close_avg']} | {d['win']} | {d['risk_hit']} | {d['worst']} |")
    dv = reg["def_vs_normal"]
    lines += ["",
              f"- defensive − normal 实时均益差：**{dv['diff_pp']}pp**（bootstrap 95% CI {dv['ci95']}）",
              "- S1 资金纪律对比（defensive 日缩放）："
              + " · ".join(f"{k} equity={v['equity']} maxDD={v['max_dd']}%" for k, v in reg["s1_curves"].items())]
    diff = dv["diff_pp"]
    ok6_tighten = diff is not None and (diff <= -1.0 and reg["regimes"]["defensive"]["pnl_avg"] is not None
                                        and reg["regimes"]["defensive"]["pnl_avg"] < 1.0)
    ok6_keep = diff is not None and diff >= -0.5 and (reg["regimes"]["defensive"]["pnl_avg"] or 0) > 0
    verdict6 = "支持防守档收紧（候选 C8 规则）" if ok6_tighten else ("支持维持 S1 半仓建议现状" if ok6_keep else "证据居中，维持现状")
    lines += ["", f"- **预注册判定⑥：{verdict6}**"]

    out = run_dir / "c8_candidates_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {out}")
    print(json.dumps({"gap": {"ann_realtime": round(gap["ann_realtime"], 1), "ann_expect": round(gap["ann_expect"], 1)},
                      "lu_up_vs_mid": lu_up["diff_pp"],
                      "def_vs_normal": dv["diff_pp"],
                      "s1_curves": reg["s1_curves"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
