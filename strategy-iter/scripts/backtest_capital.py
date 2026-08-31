# -*- coding: utf-8 -*-
"""C_Final 资金曲线回测：把「入选样本的条件期望」换算成「纪律执行的组合资金曲线」。

背景（2026-09-04）：rounds_log / 最终方案里的胜率、均次日 +2.13% 都是入选样本的
**条件期望**（T+1 收盘 vs T 收盘），不是可执行收益——它不含买点触发率（C3 全程仅
33.75%）、不含未触发日的空仓、不含开盘价为入场成本。本脚本用同一份 run 的
validation.csv，按冻结执行层（C3 buys，= speculate._MF_BUY 同口径）模拟逐日组合。

执行近似口径（日线 bar 无法还原盘中路径，各处近似在报告中原样披露）：

主口径 = 实时可执行近似（无未来函数，信息按盘中可得顺序使用）：
- 涨停组：开盘涨幅 ∈ [0,+5]% 开盘市价买入（低开不接、高开>+5% 放弃 = 实时可判）；
  执行层「盘中最低≥昨收-3% 才买」实时不可知，近似为 **-3% 破位即离场**（小止损，
  因而 -5% 大止损在本组几乎不触发）；未破位收盘离场。
- 低吸组：限价单挂在 max(开盘,昨收)（"收盘站上 max(开盘,昨收) 才买"的实时版本 =
  价格上穿该位即成交，用 T+1 最高价≥挂单价判定是否成交）；全天最低 ≤ -4%（风险位）
  按止损优先离场（保守：日线无法区分止损与成交的盘中先后）；否则收盘离场。
- 每日等权：当日成交标的均分当日资金，未成交/缺席槽位闲置（收益 0）；
  组合日收益 = 成交标的收益均值；按 T+1 交易日复利；年化按 244 交易日；费前。

对照口径 = 验证器触发判定（buy_triggered：含「收盘>开盘」等收盘才可知的确认条件，
**含日线回看、不可实盘**），仅作为「验证器触发样本统计」与可执行曲线之间差距的
上界参照——差距本身就是「确认条件的后视成分有多大」的度量。

选股单一来源：本脚本不重新选股，只消费 engine run 的 validation.csv（选股、环境、
触发、OHLC 全部来自引擎产出），与回填验证闭环零重复实现。

用法：
    python strategy-iter/scripts/backtest_capital.py --run runs/concept_round3_C3
输出：<run>/capital_report.md + <run>/capital_curve.csv；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

TRADING_DAYS_PER_YEAR = 244

# 冻结执行层（C3 buys，engine/rules.py M3 = speculate._MF_BUY 同口径）
LU = {"win_min": 0.0, "win_max": 5.0, "low_min": -3.0}
NLU = {"win_min": -2.0, "win_max": 3.0, "risk_low": -4.0}


def load_validation(run_dir):
    """validation.csv → [row dict]（数值列转 float，缺失为 None）。"""
    fp = Path(run_dir) / "validation.csv"
    out = []
    with open(fp, encoding="utf-8-sig", newline="") as f:
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
                "o": num("open_ret"), "h": num("high_ret"), "l": num("low_ret"),
                "c": num("close_ret"),
                "buy_triggered": (r.get("buy_triggered") or "").strip() == "True",
            })
    return out


def trade_pnl(row, mode="realtime"):
    """单票执行近似收益（小数）。不成交/数据缺失返回 None。

    mode="realtime"   主口径：盘中信息顺序可得（见模块头注）。
    mode="confirm"    对照口径：验证器触发判定（含收盘确认回看，不可实盘）。
    """
    o, h, l, c = row["o"], row["h"], row["l"], row["c"]
    if o is None or c is None:
        return None
    if row["group"] == "lu":
        if not (LU["win_min"] <= o <= LU["win_max"]):
            return None                      # 低开不接 / 高开>+5% 放弃（开盘即可判）
        if mode == "confirm":
            if l is None or l < LU["low_min"]:
                return None                  # 回看：全天破 -3% 的直接不买
            if not (c > o):
                return None                  # 回看：收盘确认才买
            entry, exit_pct = o, c
        else:
            entry = o
            exit_pct = LU["low_min"] if (l is not None and l < LU["low_min"]) else c
        return (1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1
    # 低吸组：限价挂在 max(开盘,昨收)
    if not (NLU["win_min"] <= o <= NLU["win_max"]):
        return None
    entry = max(o, 0.0)
    if mode == "confirm":
        if not (c > entry):
            return None                      # 回看：收盘未站上 = 全天不成交
    else:
        if h is None or h < entry:
            return None                      # 实时：当日价格未触及挂单价 = 未成交
    exit_pct = NLU["risk_low"] if (l is not None and l <= NLU["risk_low"]) else c
    return (1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1


def build_curve(rows, mode="realtime"):
    """按 T+1 日聚合等权组合 → (daily list, equity, max_dd)。

    daily: [{T1, n_trade, n_pick, day_ret, equity, trades:[(code,name,group,pnl%)]}]
    """
    by_day = {}
    for r in rows:
        by_day.setdefault(r["T1"], []).append(r)
    daily = []
    eq = 1.0
    peak, max_dd = 1.0, 0.0
    for t1 in sorted(by_day):
        picks = sorted(by_day[t1], key=lambda r: r["thscode"])
        pnls = []
        for r in picks:
            p = trade_pnl(r, mode=mode)
            if p is not None:
                pnls.append((r, p))
        day_ret = sum(p for _r, p in pnls) / len(pnls) if pnls else 0.0
        eq *= 1 + day_ret
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
        daily.append({"T1": t1, "n_pick": len(picks), "n_trade": len(pnls),
                      "day_ret": day_ret, "equity": eq,
                      "trades": [(r["thscode"], r["name"], r["group"],
                                  round(p * 100, 2)) for r, p in pnls]})
    return daily, eq, max_dd


def _stats_block(daily, eq, max_dd, rows, mode="realtime"):
    n_days = len(daily)
    traded = [d for d in daily if d["n_trade"]]
    ann = (eq ** (TRADING_DAYS_PER_YEAR / n_days) - 1) if n_days else 0.0
    pos_days = [d["day_ret"] for d in traded]
    cs = [r["c"] for r in rows if r["c"] is not None]
    pnls = [p for p in (trade_pnl(r, mode=mode) for r in rows) if p is not None]
    triggered = [r for r in rows if r["buy_triggered"]]
    return {
        "days": n_days, "traded_days": len(traded),
        "exposure": round(100.0 * len(traded) / n_days, 1) if n_days else 0.0,
        "total_pct": round((eq - 1) * 100, 2),
        "ann_pct": round(ann * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "calmar": round(ann / abs(max_dd), 2) if max_dd < 0 else None,
        "traded_day_win": round(100.0 * sum(1 for x in pos_days if x > 0) / len(pos_days), 1)
                          if pos_days else None,
        "traded_day_avg": round(100.0 * sum(pos_days) / len(pos_days), 3) if pos_days else None,
        "n_picks": len(rows), "n_trades": sum(d["n_trade"] for d in daily),
        "cond_avg_close": round(sum(cs) / len(cs), 2) if cs else None,
        "cond_win": round(100.0 * sum(1 for x in cs if x > 0) / len(cs), 2) if cs else None,
        "trigger_rate": round(100.0 * len(triggered) / len(rows), 2) if rows else None,
        "avg_trade_pnl": round(100.0 * sum(pnls) / len(pnls), 3) if pnls else None,
        "trade_win": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 2)
                     if pnls else None,
    }


def monthly_table(daily):
    """按 T+1 月聚合：月收益 = 月内日收益复利。"""
    out = []
    bym = {}
    for d in daily:
        bym.setdefault(d["T1"][:7], []).append(d)
    for m in sorted(bym):
        eq = 1.0
        for d in bym[m]:
            eq *= 1 + d["day_ret"]
        out.append({"month": m, "ret_pct": round((eq - 1) * 100, 2),
                    "traded_days": sum(1 for d in bym[m] if d["n_trade"]),
                    "days": len(bym[m])})
    return out


def group_table(rows, mode="realtime"):
    """分组成交笔数与单笔均益。"""
    out = []
    for grp, cn in (("lu", "涨停组"), ("nlu", "低吸组")):
        sub = [r for r in rows if r["group"] == grp]
        pnls = [p for p in (trade_pnl(r, mode=mode) for r in sub) if p is not None]
        out.append({"group": cn, "picks": len(sub), "trades": len(pnls),
                    "avg_pnl": round(100 * sum(pnls) / len(pnls), 3) if pnls else None,
                    "win": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1)
                           if pnls else None})
    return out


def render_report(run_name, st, stc, monthly, groups, groups_c):
    f1 = lambda v: "-" if v is None else f"{v:+.2f}%"
    f3 = lambda v: "-" if v is None else f"{v:+.3f}%"
    lines = [f"# {run_name} 资金曲线回测（纪律执行口径）", "",
             f"验证区间 {st['days']} 个 T+1 交易日；执行近似与费前口径见脚本头注。"
             "选股/环境/OHLC 全部来自引擎 validation.csv，本脚本不重新选股。", "",
             "## 组合层面（主口径：实时可执行近似，无未来函数）", "",
             "```text",
             f"累计收益 {f1(st['total_pct'])}（{st['traded_days']}/{st['days']} 日有持仓，敞口 {st['exposure']}%）",
             f"年化 {f1(st['ann_pct'])} · 最大回撤 {f1(st['max_dd_pct'])} · Calmar {st['calmar']}",
             f"有持仓日 胜率 {st['traded_day_win']}% · 日均 {f3(st['traded_day_avg'])}",
             f"入选 {st['n_picks']} 笔 → 成交 {st['n_trades']} 笔 · 单笔均益 {f3(st['avg_trade_pnl'])}"
             f" · 单笔胜率 {st['trade_win']}%",
             "```", "",
             "## 与条件期望对照（资金曲线为什么远低于「均次日 +2.13%」）", "",
             "```text",
             f"入选样本条件期望（close vs 昨收）：胜率 {st['cond_win']}% · 均次 {f1(st['cond_avg_close'])}"
             f" · 触发率 {st['trigger_rate']}%",
             f"实盘近似成交单：胜率 {st['trade_win']}% · 均笔 {f3(st['avg_trade_pnl'])}",
             "差异来源：①执行层买点纪律——开盘出窗/低吸未上穿/破位的样本按纪律根本不买；",
             "②入场成本是 T+1 开盘价（或上穿价）而非昨收——高开与低吸折价部分是给不到的利润；",
             "③验证器「触发 +5.76%」类统计含收盘确认的回看成分，不是可执行收益。",
             "```", "",
             "## 对照口径（验证器触发判定：含收盘确认回看，不可实盘，仅作上界参照）", "",
             "```text",
             f"累计 {f1(stc['total_pct'])} · 年化 {f1(stc['ann_pct'])} · 回撤 {f1(stc['max_dd_pct'])}"
             f" · 成交 {stc['n_trades']} 笔 · 均笔 {f3(stc['avg_trade_pnl'])}",
             f"与主口径的差距 = 确认条件的后视成分（实盘不可获得）。",
             "```", "",
             "## 分组（主口径 / 对照口径）", "",
             "| 组 | 入选 | 成交 | 单笔均益 | 单笔胜率 | 对照成交 | 对照均笔 |",
             "|---|---|---|---|---|---|---|"]
    for g, gc in zip(groups, groups_c):
        lines.append(f"| {g['group']} | {g['picks']} | {g['trades']} | {f3(g['avg_pnl'])} "
                     f"| {g['win'] if g['win'] is not None else '-'}% "
                     f"| {gc['trades']} | {f3(gc['avg_pnl'])} |")
    lines += ["", "## 月度（主口径）", "", "| 月 | 月收益 | 有持仓日/交易日 |", "|---|---|---|"]
    for m in monthly:
        lines.append(f"| {m['month']} | {f1(m['ret_pct'])} | {m['traded_days']}/{m['days']} |")
    lines += ["", "> 日线口径近似：涨停组「盘中最低≥昨收-3% 才买」实时不可知，近似为 -3% 破位即离场"
              "（因此 -5% 大止损在本组几乎不触发）；低吸组限价单成交用「最高价触及挂单价」判定，"
              "止损与成交的盘中先后无法区分、按止损优先（保守）。本曲线为 T+1 日内一轮口径"
              "（收盘/止损离场），持仓过 T+1 收盘之后的路径不在验证范围内；费前（不含佣金/滑点）。",
              "> 本回测为规则化模拟，非投资建议。"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="C_Final 资金曲线回测（validation.csv 消费者）")
    ap.add_argument("--run", default="runs/concept_round3_C3",
                    help="engine run 目录（含 validation.csv）")
    args = ap.parse_args(argv)
    base = Path(__file__).resolve().parent.parent          # strategy-iter/
    cand = Path(args.run)
    if cand.is_absolute():
        run_dir = cand
    else:   # 兼容「runs/xxx」（相对 strategy-iter）与「strategy-iter/runs/xxx」（相对仓库根）
        run_dir = cand if cand.exists() else base / args.run
    if not (run_dir / "validation.csv").is_file():
        print(f"找不到 {run_dir}/validation.csv（--run 应指向含 validation.csv 的 engine run 目录）")
        return 1
    rows = load_validation(run_dir)
    if not rows:
        print(f"validation.csv 为空：{run_dir}")
        return 1
    daily, eq, max_dd = build_curve(rows, mode="realtime")
    st = _stats_block(daily, eq, max_dd, rows, mode="realtime")
    daily_c, eq_c, dd_c = build_curve(rows, mode="confirm")
    stc = _stats_block(daily_c, eq_c, dd_c, rows, mode="confirm")
    monthly = monthly_table(daily)
    groups = group_table(rows, mode="realtime")
    groups_c = group_table(rows, mode="confirm")
    report = render_report(run_dir.name, st, stc, monthly, groups, groups_c)
    out_md = run_dir / "capital_report.md"
    out_md.write_text(report, encoding="utf-8")
    with open(run_dir / "capital_curve.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["T1", "picks", "trades", "day_ret_pct", "equity", "trades_detail"])
        for d in daily:
            w.writerow([d["T1"], d["n_pick"], d["n_trade"],
                        round(d["day_ret"] * 100, 4), round(d["equity"], 6),
                        " ".join(f"{c}:{p:+.2f}" for c, _n, _g, p in d["trades"])])
    print(f"== {run_dir.name} 资金曲线（实时可执行口径） ==")
    print(f"累计 {st['total_pct']:+.2f}% · 年化 {st['ann_pct']:+.2f}% · "
          f"回撤 {st['max_dd_pct']:+.2f}% · 敞口 {st['exposure']}%")
    print(f"条件期望对照：胜率 {st['cond_win']}% / 均次 {st['cond_avg_close']:+}% · "
          f"成交单均笔 {st['avg_trade_pnl']:+.3f}%（验证器触发率 {st['trigger_rate']}%）")
    print(f"对照口径（含回看，不可实盘）：累计 {stc['total_pct']:+.2f}%")
    print(f"-> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
