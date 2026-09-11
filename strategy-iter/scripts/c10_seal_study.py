# -*- coding: utf-8 -*-
"""C10 候选研究：涨停组「封板日续持」——右尾的可执行获取方式（单变量）。

背景（2026-09-10，rounds_log §6/§7/§8）：
  - 止盈族否证：固定止盈砍掉的右尾（涨停延续/回封）恰是收益来源；
  - 止损族过线依赖少数「深V回封」样本，口径对齐后不过线；
  - §7 遗留优先级②：「右尾的可执行获取方式（封板日持仓处置）」单变量检验。
  与止盈族的区别：不是在浮盈里主动砍，而是**收盘确认封板后把离场推迟一天**——
  决策点在 T+1 收盘集合竞价（封板状态当场可知，可执行），不用盘中信息。

本脚本在 C10 窗口（选股 2026-01-02..2026-09-10，T+1 结局至 2026-09-11）、C7 冻结
选股口径上，只改「已成交且 T+1 收盘封板」样本的离场日：

候选族（跑数前预注册）：
  seal_hold       封板收 → 持有至 T+2 收盘离场（T+2 无盘中止损）
  seal_hold_stop  封板收 → 持有至 T+2，盘中 ≤ risk_low(-5%) 按 -5% 离场，否则 T+2 收盘
基线 = C7 现行（执行口径对齐后）：risk_low=-5% 破位离场，否则 T+1 收盘离场。

口径与近似（沿用 backtest_capital / c9 系列，报告原样披露）：
  - 封板判定用原始价格精确判定：T+1 收盘 == round(T0 收盘 × 1.1, 2)（入选仅沪深
    主板，统一 10%）；不用 9.9% 经验阈值。
  - 续持样本限定「基线未被 risk_low 止损」的样本：T+1 盘中已破位离场的仓位在破位
    时刻即出局，事后无法「知道」尾盘回封——按可执行口径不参与续持。
  - T+2 收益以 T+1 收盘为基准（t2 close/low 相对 T1 收盘的 pct）；组合曲线仍把该笔
    记在 T+1 日（资金实际多占用一个交易日，曲线口径披露，不作调整）。
  - T+2 无 bar（T1=窗口末/停牌）→ 该样本变体=基线（不配对、单列披露 n）。

预注册判定（采纳需 ①②③④ 全部满足，样本 = 涨停组已成交且有 T+2）：
  ① 实时均益差（变体 − 基线）≥ +0.30pp；
  ② 逐样本配对 bootstrap 95% CI（BOOT_N=2000, seed=7）下界 > 0；
  ③ 窗口前后两半方向一致（两半均益差均 > 0）；
  ④ 组合实时曲线 equity ≥ 基线，且最大回撤不深于基线 0.5pp 以上。
  多重检验披露：本族是「离场管理」主题的第三族（止盈、止损之后），任何过线结论
  按轮次纪律须连续两轮同向复核后方可进入规则。

口径：选股单一来源 = run 的 validation.csv；执行层常量单一来源 = backtest_capital；
T+2 行情单一来源 = data/daily_raw.parquet（原始价，封板判定与 T+2 pct 同源）；
收益费前。用法：
    python strategy-iter/scripts/c10_seal_study.py --run runs/c10_window_C7
输出：<run>/c10_seal_report.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import backtest_capital as bt   # noqa: E402  执行口径单一来源
import c8_candidate_study as c8   # noqa: E402  统计工具/装载复用
import c9_tp_study as c9   # noqa: E402  paired_pnl/halves_of/build_curve_pnl/verdict_of 复用

CANDIDATES = ("seal_hold", "seal_hold_stop")
_SEAL_TOL = 0.005   # 封板价比较容差（半个最小变动价位；非封板收盘距 limit ≥ 1 tick）


# ---------------------------------------------------------------- 数据装载

class T2Bars:
    """T+2 行情查询：T+1 收盘价 → T+2 close/low 相对 T+1 收盘的 pct；封板精确判定。

    prev_close 列在导出 parquet 中为空，用组内上一根 bar 的 close 推（停牌缺口时
    上一根即上一交易日，与交易所涨停基准一致）。"""

    def __init__(self):
        df = pd.read_parquet(BASE / "data" / "daily_raw.parquet",
                             columns=["thscode", "date", "low", "close"])
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values(["thscode", "date"])
        self._idx: dict[str, dict[str, int]] = {}
        self._prev: dict[str, list[float | None]] = {}
        self._rows: dict[str, list[dict]] = {}
        for code, g in df.groupby("thscode"):
            closes = g["close"].astype(float).tolist()
            rows = [{"date": d, "low": float(l), "close": c}
                    for d, l, c in zip(g["date"], g["low"], closes)]
            prev = [None] + closes[:-1]
            self._rows[code] = rows
            self._prev[code] = prev
            self._idx[code] = {d: i for i, d in enumerate(g["date"])}

    def t1_info(self, code: str, t1: str) -> dict | None:
        """T+1 当日 (close, 是否收盘封板)；无 bar 返回 None。"""
        rows = self._rows.get(code)
        i = self._idx.get(code, {}).get(t1)
        if rows is None or i is None:
            return None
        prev = self._prev[code][i]
        if prev is None or prev <= 0:
            return {"close": rows[i]["close"], "sealed": False}
        limit = round(prev * 1.1, 2)
        return {"close": rows[i]["close"], "sealed": abs(rows[i]["close"] - limit) < _SEAL_TOL}

    def t2_pcts(self, code: str, t1: str) -> dict | None:
        """T+2 的 close/low 相对 T+1 收盘的 pct；无 T+2 bar（窗口末/停牌）返回 None。"""
        rows = self._rows.get(code)
        i = self._idx.get(code, {}).get(t1)
        if rows is None or i is None or i + 1 >= len(rows):
            return None
        nxt, base = rows[i + 1], rows[i]["close"]
        return {"c": (nxt["close"] / base - 1) * 100.0,
                "l": (nxt["low"] / base - 1) * 100.0}


def is_stopped(r: dict) -> bool:
    """基线在 T+1 是否被 risk_low 止损（与 bt.trade_pnl 同一判定）。"""
    return r["l"] is not None and r["l"] <= bt.LU["risk_low"]


# ---------------------------------------------------------------- 变体收益

def seal_pnl(r: dict, t2: dict | None, t2_stop: bool) -> float | None:
    """封板续持单票收益（小数）。t2=None（无 T+2）回落为基线收盘离场。

    前置（续持资格）由 caller 保证：已成交、未被 risk_low 止损、T+1 收盘封板。"""
    o = r["o"]
    if t2 is None:
        return (1 + r["c"] / 100.0) / (1 + o / 100.0) - 1
    if t2_stop and t2["l"] <= bt.LU["risk_low"]:
        return (1 + bt.LU["risk_low"] / 100.0) / (1 + o / 100.0) - 1
    return (1 + t2["c"] / 100.0) / (1 + o / 100.0) - 1


def build_variant(bars: T2Bars, t2_stop: bool):
    """组合口径 pnl：封板续持变体，其余（含低吸组）与基线逐字一致。"""
    cache: dict[tuple, dict | None] = {}

    def fn(r: dict):
        if r["group"] != "lu":
            return bt.trade_pnl(r, mode="realtime")
        b = bt.trade_pnl(r, mode="realtime")
        if b is None:
            return None
        key = (r["thscode"], r["T1"])
        if key not in cache:
            cache[key] = bars.t1_info(*key)
        info = cache[key]
        if info is None or not info["sealed"] or is_stopped(r):
            return b
        k2 = (r["thscode"], "t2", r["T1"])
        if k2 not in cache:
            cache[k2] = bars.t2_pcts(r["thscode"], r["T1"])
        return seal_pnl(r, cache[k2], t2_stop)
    return fn


def carried_counts(lu_rows: list[dict], bars: T2Bars):
    """续持资格计数：已成交中「封板+未止损」（可续持）与其中无 T+2 的笔数。"""
    carried = no_t2 = 0
    for r in lu_rows:
        if bt.trade_pnl(r, mode="realtime") is None or is_stopped(r):
            continue
        info = bars.t1_info(r["thscode"], r["T1"])
        if info is None or not info["sealed"]:
            continue
        carried += 1
        if bars.t2_pcts(r["thscode"], r["T1"]) is None:
            no_t2 += 1
    return carried, no_t2


def top_examples(lu_rows: list[dict], bars: T2Bars, t2_stop: bool, k: int = 5):
    """续持样本的差值明细（前后各 k，事后披露不进判定）。"""
    out = []
    fn = build_variant(bars, t2_stop)
    for r in lu_rows:
        b = bt.trade_pnl(r, mode="realtime")
        if b is None or is_stopped(r):
            continue
        info = bars.t1_info(r["thscode"], r["T1"])
        if info is None or not info["sealed"]:
            continue
        t2 = bars.t2_pcts(r["thscode"], r["T1"])
        if t2 is None:
            continue
        out.append({"T1": r["T1"], "code": r["thscode"], "name": r.get("name", ""),
                    "base_pp": round(b * 100, 2), "var_pp": round(fn(r) * 100, 2)})
    out.sort(key=lambda x: x["var_pp"] - x["base_pp"])
    return out[-k:][::-1], out[:k]


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="C10 候选研究：涨停组封板日续持（单变量）")
    ap.add_argument("--run", required=True, help="engine run 目录（含 validation.csv）")
    args = ap.parse_args()
    run_dir = Path(args.run) if Path(args.run).is_absolute() else BASE / args.run

    picks = c8.load_picks(run_dir)
    lu = c9.lu_only(picks)
    bars = T2Bars()
    base_curve = c9.build_curve_pnl(picks, lambda r: bt.trade_pnl(r, mode="realtime"))
    base_lu = [p for p in (bt.trade_pnl(r, mode="realtime") for r in lu) if p is not None]

    stats, verdicts = {}, {}
    for name in CANDIDATES:
        fn = build_variant(bars, t2_stop=(name == "seal_hold_stop"))
        base, var, diffs = c9.paired_pnl(lu, fn)          # 总体配对：全体已成交（未续持样本差=0）
        _daily, eq, dd = c9.build_curve_pnl(picks, fn)
        carried, no_t2 = carried_counts(lu, bars)
        st = {"n_exec": len(base), "n_carried": carried, "n_no_t2": no_t2,
              "pnl_base": round(c8._mean(base) * 100, 3) if base else None,
              "pnl_var": round(c8._mean(var) * 100, 3) if var else None,
              "diff_pp": round(c8._mean(diffs) * 100, 3) if diffs else None,
              "ci95": c9._paired_ci(diffs),
              "halves": c9.halves_of(lu, fn),
              "eq_base": round(base_curve[1], 4), "dd_base": round(base_curve[2] * 100, 1),
              "eq_var": round(eq, 4), "dd_var": round(dd * 100, 1)}
        stats[name] = st
        verdicts[name] = c9.verdict_of(st)

    top, bottom = top_examples(lu, bars, t2_stop=False)

    n_days = len({r["T1"] for r in picks})
    ann = lambda eq: (eq ** (bt.TRADING_DAYS_PER_YEAR / n_days) - 1) * 100  # noqa: E731

    lines = ["# C10 候选研究：涨停组「封板日续持」——右尾的可执行获取方式（单变量）", "",
             f"- run：`{run_dir.name}` · picks n={len(picks)} · 涨停组已成交 n={len(base_lu)} · "
             f"T+1 交易日 {n_days}",
             "- 单一变量：只改「已成交 + 未被 risk_low 止损 + T+1 收盘封板」样本的离场日；"
             "选股/配额/门槛/环境/低吸组与其余离场全部与 C7 冻结一致",
             "- 封板判定 = T+1 原始收盘价 == round(T0 收盘 × 1.1, 2)（入选仅沪深主板，统一 10%）",
             "- 本族是离场管理主题第三族（止盈/止损之后）——多重检验披露；收益费前", "",
             "## 基线复现（C7 现行：risk_low -5% 破位离场，否则 T+1 收盘）", "",
             f"- 涨停组已成交实时均益 **{c8._mean(base_lu) * 100:+.3f}%** · 单笔胜率 {c8._win(base_lu)}%",
             f"- 组合实时曲线：equity **{base_curve[1]:.4f}** · 年化 {ann(base_curve[1]):+.1f}% · "
             f"最大回撤 {base_curve[2] * 100:.1f}%", "",
             "## 候选对比（配对：仅「有 T+2 的续持样本」进入差值集合）", "",
             "| 变体 | 续持/无T+2/已成交 | 实时均益% | 差pp | 配对CI95 | 前半/后半diff | 组合equity | 组合maxDD% | 判定 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name in CANDIDATES:
        st = stats[name]
        lines.append(
            f"| {name} | {st['n_carried']}/{st['n_no_t2']}/{st['n_exec']} | {st['pnl_var']} "
            f"| {st['diff_pp']:+} | {st['ci95']} | "
            + "/".join(str(h["diff_pp"]) for h in st["halves"])
            + f" | {st['eq_var']} | {st['dd_var']} | {'**过线**' if verdicts[name] else '不过线'} |")

    lines += ["", "## 预注册判定", ""]
    if any(verdicts.values()):
        passed = [n for n in CANDIDATES if verdicts[n]]
        st = stats["seal_hold"]
        lines += [f"- 过线候选：{', '.join(passed)}（判定附表）；seal_hold 差 {st['diff_pp']:+}pp，CI95 {st['ci95']}",
                  "- **判定：成立——按轮次纪律须连续两轮同向复核后方可进入规则**"]
    else:
        lines += ["- 无候选满足 ①②③④ 全部条件",
                  "- **判定：不成立——维持「T+1 收盘离场」铁律；右尾不可用日线级规则低成本获取**"]

    lines += ["", "## 机制明细（seal_hold，事后披露不进判定）", "",
              f"- 续持样本 {stats['seal_hold']['n_carried']} 笔（T+2 缺失 {stats['seal_hold']['n_no_t2']} 笔按基线计）；"
              f"变体均益 {stats['seal_hold']['pnl_var']}% vs 基线 {stats['seal_hold']['pnl_base']}%",
              f"- 续持改善前 {len(top)} 笔：" + "；".join(
                  f"{x['T1']} {x['code']} {x['name']} {x['base_pp']}→{x['var_pp']}" for x in top),
              f"- 续持变差前 {len(bottom)} 笔：" + "；".join(
                  f"{x['T1']} {x['code']} {x['name']} {x['base_pp']}→{x['var_pp']}" for x in bottom), "",
              "## 口径与近似披露", "",
              "- 决策点为 T+1 收盘集合竞价的封板状态（当场可知、可执行），不用盘中信息；",
              "- T+2 无 bar（窗口末 T1=2026-09-11 / 停牌）按基线计并单列，不进配对差；",
              "- 组合曲线把续持笔记在 T+1 日（资金实际多占用一日，未按日历重排，判定④同口径公平）；",
              "- T+2 涨跌停一字无法卖出的极端情形日线不可辨（与基线同一近似）；费前，非投资建议。"]
    out = run_dir / "c10_seal_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {out}")
    print(json.dumps({"n_lu_exec": len(base_lu),
                      "stats": stats,
                      "verdicts": verdicts},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
