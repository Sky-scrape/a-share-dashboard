# -*- coding: utf-8 -*-
"""竞价/开盘缺口前瞻回验：T+1 早盘竞价信息能否缓解「指数急跌闸门 1 日滞后」。

背景（2026-09-04，C_Final 遗留观察项④）：T 日收盘选股只用 ≤T 数据，系统性下跌
往往 T+1 才在指数上确认（闸门滞后 1 日，2026-09 初样本 -2.77%）。但 T+1 09:25
竞价结束时，当日开盘缺口已经全场可知——本回验回答：**T+1 竞价缺口信号对「当日
备选池执行收益」有没有区分度**，值不值得作为执行层的前瞻提示（注意：只作提示，
不改环境配额/结构规则——那需要走系统文档的完整迭代纪律）。

信号（全部可用 T+1 09:25 前的当日数据计算，与竞价页口径对应）：
- S1 全市场开盘缺口中位数（DuckDB/日线口径；竞价页近似 = 板块竞价强度中位）；
- S2 昨日涨停池（T 日涨停，主板 10cm ≥9.7% / 20cm ≥19.7% 近似）今日竞价缺口：
  低开占比（缺口<0）与深低开占比（≤-2%）——竞价页「涨停接力」面板同视角。

结局：当日（T+1）C_Final 备选池按回测脚本实时可执行口径的等权日收益
（backtest_capital.trade_pnl/build_curve 同一实现，选股单一来源=引擎 validation.csv）。

用法：
    python strategy-iter/scripts/gap_ahead_study.py --run runs/concept_round3_C3
输出：<run>/gap_ahead_report.md；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import backtest_capital as bt  # noqa: E402  同目录：执行口径单一来源

# S2 涨停判定（主板 10cm / 创业科创 20cm 近似，北交所不在口径内）
LU_TH_10, LU_TH_20 = 9.7, 19.7


def _is_main(code):
    return str(code)[:3] in ("600", "601", "603", "605", "000", "001", "002", "003")


def _is_bj(code):
    return str(code).endswith(".BJ")


def load_gaps():
    """全市场逐日开盘缺口长表：[thscode, date, gap%] + [date, 昨日是否涨停]。"""
    df = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
    df = df[~df["thscode"].map(_is_bj)]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")  # 引擎同款归一
    df = df.sort_values(["thscode", "date"])
    g = df.groupby("thscode")
    df["prev_close"] = g["close"].shift(1)
    df["pct"] = (df["close"] / df["prev_close"] - 1) * 100
    df["gap"] = (df["open"] / df["prev_close"] - 1) * 100
    # 昨日涨停（含 20cm；次新无 prev_close 自然为 NaN）
    df["prev_lu"] = df["pct"] >= pd.Series(
        [LU_TH_20 if str(c)[:2] in ("30", "68") else LU_TH_10 for c in df["thscode"]],
        index=df.index)
    return df[["thscode", "date", "gap", "prev_lu"]].dropna(subset=["gap"])


def day_signals(gaps, day):
    """单日（T+1）竞价信号：{med_gap, lu_gap_mean, lu_open_down, lu_open_deep, lu_n}。"""
    d = gaps[gaps["date"] == day]
    if d.empty:
        return None
    lu = d[d["prev_lu"]]
    return {
        "med_gap": round(float(d["gap"].median()), 3),
        "lu_gap_mean": round(float(lu["gap"].mean()), 3) if len(lu) else None,
        "lu_open_down": round(float((lu["gap"] < 0).mean()), 3) if len(lu) else None,
        "lu_open_deep": round(float((lu["gap"] <= -2).mean()), 3) if len(lu) else None,
        "lu_n": int(len(lu)),
    }


def day_outcome(rows, day):
    """当日备选池实时口径等权日收益（%）与入选数；无池返回 None。"""
    sub = [r for r in rows if r["T1"] == day]
    if not sub:
        return None
    pnls = [p for p in (bt.trade_pnl(r, mode="realtime") for r in sub) if p is not None]
    cond = [r["c"] for r in sub if r["c"] is not None]
    return {
        "n_pick": len(sub), "n_trade": len(pnls),
        "day_ret": round(100.0 * sum(pnls) / len(pnls), 3) if pnls else None,
        "cond_avg": round(sum(cond) / len(cond), 3) if cond else None,
    }


def bucket_table(days, key, edges, labels):
    """按信号分桶：[{label, n, avg_ret, avg_cond, worst, days:[(day,ret)]}]。"""
    out = []
    for lab, lo, hi in zip(labels, edges[:-1], edges[1:]):
        sub = [d for d in days if d["sig"][key] is not None and lo <= d["sig"][key] < hi]
        rets = [d["out"]["day_ret"] for d in sub if d["out"] and d["out"]["day_ret"] is not None]
        conds = [d["out"]["cond_avg"] for d in sub if d["out"] and d["out"]["cond_avg"] is not None]
        worst = min(rets) if rets else None
        out.append({
            "label": lab, "n": len(sub),
            "avg_ret": round(sum(rets) / len(rets), 3) if rets else None,
            "avg_cond": round(sum(conds) / len(conds), 3) if conds else None,
            "worst": worst,
            "neg_days": sum(1 for x in rets if x < 0),
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="竞价缺口前瞻回验（C_Final 遗留观察项④）")
    ap.add_argument("--run", default="runs/concept_round3_C3")
    args = ap.parse_args(argv)
    cand = Path(args.run)
    run_dir = cand if cand.is_absolute() else \
        (cand if cand.exists() else BASE / args.run)
    rows = bt.load_validation(run_dir)
    gaps = load_gaps()
    days = []
    for t1 in sorted({r["T1"] for r in rows}):
        sig = day_signals(gaps, t1)
        if sig is None:
            continue
        days.append({"day": t1, "sig": sig, "out": day_outcome(rows, t1)})

    med_buckets = bucket_table(days, "med_gap",
                               [-99, -0.5, 0.0, 0.5, 99],
                               ["缺口中位<-0.5%", "-0.5~0%", "0~+0.5%", ">+0.5%"])
    down_buckets = bucket_table(days, "lu_open_down",
                                [-1, 0.3, 0.5, 0.7, 2],
                                ["昨涨停低开占比<30%", "30~50%", "50~70%", ">70%"])
    deep_buckets = bucket_table(days, "lu_open_deep",
                                [-1, 0.1, 0.25, 0.4, 2],
                                ["昨涨停深低开(≤-2%)<10%", "10~25%", "25~40%", ">40%"])

    lines = [f"# {run_dir.name} 竞价/开盘缺口前瞻回验", "",
             f"样本 {len(days)} 个 T+1 交易日；信号全部可由 T+1 09:25 前当日数据计算；"
             "结局 = 当日 C_Final 备选池实时可执行口径等权日收益（backtest_capital 同实现）。"
             "涨停池为日线近似（10cm≥9.7%/20cm≥19.7%），与竞价页昨日涨停池同视角。", "",
             "## S1 全市场开盘缺口中位数 → 当日备选池收益", "",
             "| 桶 | 天数 | 备选池日均益 | 条件期望均次 | 最差日 | 亏损日占比 |", "|---|---|---|---|---|---|"]
    for b in med_buckets:
        neg = f"{round(100 * b['neg_days'] / b['n'])}%" if b["n"] else "-"
        lines.append(f"| {b['label']} | {b['n']} | {b['avg_ret']}% | {b['avg_cond']}% "
                     f"| {b['worst']}% | {neg} |")
    lines += ["", "## S2 昨日涨停池竞价低开占比 → 当日备选池收益", "",
              "| 桶 | 天数 | 备选池日均益 | 条件期望均次 | 最差日 | 亏损日占比 |", "|---|---|---|---|---|---|"]
    for b in down_buckets:
        neg = f"{round(100 * b['neg_days'] / b['n'])}%" if b["n"] else "-"
        lines.append(f"| {b['label']} | {b['n']} | {b['avg_ret']}% | {b['avg_cond']}% "
                     f"| {b['worst']}% | {neg} |")
    lines += ["", "## S3 昨日涨停池深低开（≤-2%）占比 → 当日备选池收益", "",
              "| 桶 | 天数 | 备选池日均益 | 条件期望均次 | 最差日 | 亏损日占比 |", "|---|---|---|---|---|---|"]
    for b in deep_buckets:
        neg = f"{round(100 * b['neg_days'] / b['n'])}%" if b["n"] else "-"
        lines.append(f"| {b['label']} | {b['n']} | {b['avg_ret']}% | {b['avg_cond']}% "
                     f"| {b['worst']}% | {neg} |")

    # 空仓规则模拟：若 S2 最差桶空仓，曲线如何变化（对照 = 不空仓）
    worst_b = min((b for b in down_buckets if b["n"] >= 10),
                  key=lambda b: (b["avg_ret"] if b["avg_ret"] is not None else 0),
                  default=None)
    if worst_b is not None:
        lo, hi = {"昨涨停低开占比<30%": (-1, 0.3), "30~50%": (0.3, 0.5),
                  "50~70%": (0.5, 0.7), ">70%": (0.7, 2)}[worst_b["label"]]
        skip = {d["day"] for d in days
                if d["sig"]["lu_open_down"] is not None and lo <= d["sig"]["lu_open_down"] < hi}
        full_daily, full_eq, full_dd = bt.build_curve(rows, mode="realtime")
        kept = [r for r in rows if r["T1"] not in skip]
        skip_daily, skip_eq, skip_dd = bt.build_curve(kept, mode="realtime")
        lines += ["", f"## 空仓规则模拟：昨日涨停池低开占比命中「{worst_b['label']}」的 {len(skip)} 日空仓", "",
                  "```text",
                  f"不空仓：累计 {(full_eq - 1) * 100:+.2f}% · 回撤 {full_dd * 100:+.2f}%",
                  f"空仓后：累计 {(skip_eq - 1) * 100:+.2f}% · 回撤 {skip_dd * 100:+.2f}%"
                  f"（收益差 {((skip_eq - full_eq) * 100):+.2f}pct · 回撤差 {((skip_dd - full_dd) * 100):+.2f}pct）",
                  f"注意：命中仅 {len(skip)} 日（小样本，且效果集中于少数大跌日），只作提示不改规则。",
                  "```"]
    lines += ["", "> 结论口径：信号分桶只在「执行层提示」层面使用（竞价页执行卡显示当日信号"
              "与历史同桶均益），不改环境配额与结构规则——那需要按《自动选股与策略自迭代系统》"
              "完整重跑迭代。涨停池/缺口均为日线近似，与真实 09:25 竞价口径存在样本差。",
              "> 本回验为规则化模拟，非投资建议。"]
    out_md = run_dir / "gap_ahead_report.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    for title, bs in (("S1 缺口中位", med_buckets), ("S2 昨涨停低开占比", down_buckets),
                      ("S3 昨涨停深低开占比", deep_buckets)):
        print(f"== {title} ==")
        for b in bs:
            print(f"  {b['label']:<18} n={b['n']:<3} 日均益 {b['avg_ret']}%  最差 {b['worst']}%")
    print(f"-> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
