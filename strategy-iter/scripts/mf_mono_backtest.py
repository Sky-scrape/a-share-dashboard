# -*- coding: utf-8 -*-
"""满仓单调变体资金曲线回测（2026-09-20）：现行策略的资金管理层单一变量对照。

背景：现行策略（C7 冻结选股 + 执行层 MF_BUY）的资金管理是「建议层 S1 环境缩仓」
——防守/冰点档半仓，实盘是否采用由使用者决定（系统文档 §0.6）。本脚本回答
「如果不用 S1、每天一律满仓（满仓单调），收益率是多少」。

策略定义（满仓单调 = M_Full，与现行策略的唯一差异在资金管理层）：
- 选股：与现行完全一致（本脚本不重新选股，只消费 engine run 的 validation.csv）；
- 执行层：与现行完全一致（backtest_capital.realtime 单一来源——涨停组开盘窗
  [0,+5]% 买入 / risk_low=-5% 破位离场；低吸组限价 max(开盘,昨收) 上穿成交 /
  risk_low=-4% 止损；否则 T+1 收盘离场）；
- 资金管理：每个交易日一律满仓——当日全部资金等权分给按纪律成交的标的，
  不做环境缩仓（S1 的 defensive/freeze 0.5x 不执行）、不做连败降仓；
  无成交标的日空仓（每日轮动制，无隔夜持仓接续，与现行一致）。
  「单调」= 仓位恒为满仓，不随市场环境/盈亏序列调节。

对照曲线（同一 trade 流，量化各层贡献）：
- S1 环境缩仓（现行建议层）：同一成交流，T 日环境 defensive/freeze 时 0.5x 仓位。
  环境取 T 日 regime（validation.csv 自带；T 日收盘后、T+1 开盘前可得，无未来函数；
  exp_hold_sizing 旧口径用 T1 日环境含轻微回看，此处修正）。
- Naive 无纪律满仓（上界参照）：入选即买——不看买点窗口、不设止损，T+1 开盘把
  全部资金等权买入当日全部入选票、收盘全卖。度量「满仓单调地买现行选股、
  完全不执行纪律」的收益率，也即执行层纪律的价值。

口径：日度复利；年化按 244 交易日；费前与费后（双边 0.15%，佣金+滑点近似，
与 exp_hold_sizing / 系统文档 §0.6 同口径）双报。

用法：
    python strategy-iter/scripts/mf_mono_backtest.py --run runs/c12_window_C7
输出：<run>/mf_mono_report.md + <run>/mf_mono_curve.csv；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

TRADING_DAYS_PER_YEAR = 244
FEE = 0.15  # 双边成本（%），与 exp_hold_sizing 同口径

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_capital import load_validation, build_curve  # noqa: E402


def curve_from_daily(daily: list[dict], size_fn=None, fee: float = 0.0) -> dict:
    """对 build_curve 的 daily（满仓成交标的口径）施加仓位系数与费后折减。

    size_fn(d) -> 仓位系数（默认恒 1.0）；费用只随实际动用的资金走——
    有成交日扣 size * fee/100，无成交日（空仓）不扣费。
    返回 {points, equity, max_dd, total, ann, ...}。
    """
    eq, peak, max_dd = 1.0, 1.0, 0.0
    points = []
    for d in daily:
        size = 1.0 if size_fn is None else size_fn(d)
        traded = d.get("n_trade", 0) > 0
        day_net = size * d["day_ret"] - (size * fee / 100.0 if traded else 0.0)
        eq *= 1 + day_net
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
        points.append({"T1": d["T1"], "size": size,
                       "traded": d.get("n_trade", 0) > 0,
                       "day_gross_pct": d["day_ret"] * 100,
                       "day_net_pct": day_net * 100, "equity": eq})
    n = len(points)
    return {"points": points, "equity": eq, "max_dd": max_dd,
            "total": eq - 1,
            "ann": eq ** (TRADING_DAYS_PER_YEAR / n) - 1 if n else 0.0}


def naive_daily(rows: list[dict]) -> list[dict]:
    """Naive 无纪律口径的逐日组合：入选即买（开盘）、收盘全卖（不止损）。

    单票 pnl = (1+close%)/(1+open%)-1（T+1 开盘买入、收盘卖出，无窗口无止损）；
    当日全部资金等权分给当日全部入选票（数据缺失票该份闲置、收益 0）。
    """
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["T1"], []).append(r)
    daily = []
    for t1 in sorted(by_day):
        picks = sorted(by_day[t1], key=lambda r: r["thscode"])
        pnls = []
        for r in picks:
            o, c = r["o"], r["c"]
            if o is None or c is None:
                continue
            pnls.append((1 + c / 100.0) / (1 + o / 100.0) - 1)
        day = sum(pnls) / len(picks) if picks else 0.0
        daily.append({"T1": t1, "n_pick": len(picks), "n_trade": len(pnls),
                      "day_ret": day,
                      "trades": [(r["thscode"], r["name"], r["group"],
                                  round(p * 100, 2))
                                 for r, p in zip(picks, pnls)]})
    return daily


def regime_size_factory(regime_by_t1: dict[str, str], half: tuple = ("defensive", "freeze")):
    """S1 环境缩仓：T 日环境 defensive/freeze → 0.5x（T 日环境 T+1 开盘前可得）。"""
    def size_fn(d: dict) -> float:
        return 0.5 if regime_by_t1.get(d["T1"], "") in half else 1.0
    return size_fn


def stats_block(curve: dict, daily: list[dict], fee: float, label: str,
                gross: dict | None = None) -> dict:
    pts = curve["points"]
    traded = [p for p, d in zip(pts, daily) if d["n_trade"]]
    out = {
        "label": label,
        "days": len(pts),
        "traded_days": len(traded),
        "exposure": round(100.0 * len(traded) / len(pts), 1) if pts else 0.0,
        "total_pct": round(curve["total"] * 100, 2),
        "ann_pct": round(curve["ann"] * 100, 2),
        "max_dd_pct": round(curve["max_dd"] * 100, 2),
        "calmar": round(curve["ann"] / abs(curve["max_dd"]), 2)
                  if curve["max_dd"] < 0 else None,
        "day_win": round(100.0 * sum(1 for p in traded if p["day_net_pct"] > 0)
                         / len(traded), 1) if traded else None,
    }
    if gross is not None:
        out["total_gross_pct"] = round(gross["total"] * 100, 2)
        out["ann_gross_pct"] = round(gross["ann"] * 100, 2)
        out["max_dd_gross_pct"] = round(gross["max_dd"] * 100, 2)
    return out


def monthly_table(points: list[dict]) -> list[dict]:
    bym: dict[str, list[dict]] = {}
    for p in points:
        bym.setdefault(p["T1"][:7], []).append(p)
    out = []
    for m in sorted(bym):
        eq = 1.0
        for p in bym[m]:
            eq *= 1 + p["day_net_pct"] / 100.0
        out.append({"month": m, "ret_pct": round((eq - 1) * 100, 2),
                    "days": len(bym[m]),
                    "full_days": sum(1 for p in bym[m]
                                     if p["size"] >= 1.0 and p.get("traded"))})
    return out


def render_report(run_name: str, window: str, st: dict, st_s1: dict,
                  st_nv: dict, monthly: list[dict], fee: float) -> str:
    f1 = lambda v: "-" if v is None else f"{v:+.2f}%"
    lines = [f"# {run_name} 满仓单调变体资金曲线回测", "",
             f"验证区间 {window}（{st['days']} 个 T+1 交易日）。策略定义与口径见脚本头注："
             "选股与执行层与现行完全一致（backtest_capital.realtime 单一来源），"
             "唯一差异 = 资金管理层每天一律满仓（不执行 S1 环境缩仓建议）。", "",
             "## 主答案：满仓单调（M_Full）", "",
             "```text",
             f"费前 累计 {f1(st['total_gross_pct'])} · 年化 {f1(st['ann_gross_pct'])} · 回撤 {f1(st['max_dd_gross_pct'])}",
             f"费后 累计 {f1(st['total_pct'])}（{st['traded_days']}/{st['days']} 日有持仓，敞口 {st['exposure']}%）",
             f"     年化 {f1(st['ann_pct'])} · 最大回撤 {f1(st['max_dd_pct'])} · Calmar {st['calmar']}",
             f"     有持仓日 胜率 {st['day_win']}%",
             "```", "",
             "## 对照：现行建议层 S1 环境缩仓（同 trade 流，defensive/freeze 0.5x）", "",
             "```text",
             f"费前 累计 {f1(st_s1['total_gross_pct'])} · 回撤 {f1(st_s1['max_dd_gross_pct'])}",
             f"费后 累计 {f1(st_s1['total_pct'])} · 年化 {f1(st_s1['ann_pct'])} · "
             f"回撤 {f1(st_s1['max_dd_pct'])} · Calmar {st_s1['calmar']}",
             f"满仓单调 − S1（费后）= {f1(round(st['total_pct'] - st_s1['total_pct'], 2))}（正=缩仓建议跑输满仓）",
             "```", "",
             "## 对照：Naive 无纪律满仓（入选即买、收盘全卖，无窗口无止损；上界参照）", "",
             "```text",
             f"费前 累计 {f1(st_nv['total_gross_pct'])} · 回撤 {f1(st_nv['max_dd_gross_pct'])}",
             f"费后 累计 {f1(st_nv['total_pct'])} · 年化 {f1(st_nv['ann_pct'])} · 回撤 {f1(st_nv['max_dd_pct'])}",
             f"满仓单调（守纪律）− Naive（费后）= {f1(round(st['total_pct'] - st_nv['total_pct'], 2))}"
             "（正=执行层纪律有正贡献）",
             "```", "",
             f"## 月度（满仓单调，费后 {fee}% 双边）", "",
             "| 月 | 月收益 | 满仓日/交易日 |", "|---|---|---|"]
    for m in monthly:
        lines.append(f"| {m['month']} | {f1(m['ret_pct'])} | {m['full_days']}/{m['days']} |")
    lines += ["", "> 口径：日线 bar 无法还原盘中路径，执行近似沿用 backtest_capital 头注"
              "（涨停组开盘窗 [0,+5]%、risk_low=-5% 破位离场；低吸组限价上穿成交、"
              "risk_low=-4% 止损优先；否则 T+1 收盘离场）；费后为双边 "
              f"{fee}%（佣金+滑点近似）。S1 环境取 T 日 regime（T+1 开盘前可得，无未来函数；"
              "exp_hold_sizing 旧口径用 T1 日环境含轻微回看）。无成交标的日空仓（每日轮动制）。"
              "本回测为规则化模拟，非投资建议。"]
    return "\n".join(lines)


def write_curve_csv(path: Path, pts_full: list[dict], pts_s1: list[dict],
                    pts_nv: list[dict]):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["T1", "full_size", "full_day_gross_pct", "full_day_net_pct",
                "full_equity", "s1_size", "s1_equity", "naive_equity"])
    for a, b, c in zip(pts_full, pts_s1, pts_nv):
        w.writerow([a["T1"], a["size"], round(a["day_gross_pct"], 4),
                    round(a["day_net_pct"], 4), round(a["equity"], 6),
                    b["size"], round(b["equity"], 6), round(c["equity"], 6)])
    path.write_text(buf.getvalue(), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="满仓单调变体资金曲线回测（validation.csv 消费者）")
    ap.add_argument("--run", default="runs/c12_window_C7",
                    help="engine run 目录（含 validation.csv）")
    ap.add_argument("--fee", type=float, default=FEE, help="双边成本%%（默认 0.15）")
    args = ap.parse_args(argv)
    base = Path(__file__).resolve().parent.parent          # strategy-iter/
    cand = Path(args.run)
    run_dir = cand if cand.is_absolute() else (cand if cand.exists() else base / args.run)
    if not (run_dir / "validation.csv").is_file():
        print(f"找不到 {run_dir}/validation.csv（--run 应指向含 validation.csv 的 engine run 目录）")
        return 1
    rows = load_validation(run_dir)
    if not rows:
        print(f"validation.csv 为空：{run_dir}")
        return 1

    # 满仓单调 / S1：同一成交流（backtest_capital.realtime 单一来源）
    daily, _eq0, _dd0 = build_curve(rows, mode="realtime")
    regime_by_t1 = {r["T1"]: r["regime"] for r in rows}
    s1_fn = regime_size_factory(regime_by_t1)
    cur_full = curve_from_daily(daily, size_fn=None, fee=args.fee)
    cur_s1 = curve_from_daily(daily, size_fn=s1_fn, fee=args.fee)
    # Naive：入选即买，另一套逐日组合
    daily_nv = naive_daily(rows)
    cur_nv = curve_from_daily(daily_nv, size_fn=None, fee=args.fee)
    # 费前对照曲线（fee=0，口径同上）
    cur_full_g = curve_from_daily(daily, size_fn=None, fee=0.0)
    cur_s1_g = curve_from_daily(daily, size_fn=s1_fn, fee=0.0)
    cur_nv_g = curve_from_daily(daily_nv, size_fn=None, fee=0.0)

    st = stats_block(cur_full, daily, args.fee, "满仓单调", gross=cur_full_g)
    st_s1 = stats_block(cur_s1, daily, args.fee, "S1 环境缩仓", gross=cur_s1_g)
    st_nv = stats_block(cur_nv, daily_nv, args.fee, "Naive 无纪律满仓", gross=cur_nv_g)
    monthly = monthly_table(cur_full["points"])
    window = f"{rows[0]['T1']} → {max(r['T1'] for r in rows)}"

    report = render_report(run_dir.name, window, st, st_s1, st_nv, monthly, args.fee)
    out_md = run_dir / "mf_mono_report.md"
    out_md.write_text(report, encoding="utf-8")
    write_curve_csv(run_dir / "mf_mono_curve.csv",
                    cur_full["points"], cur_s1["points"], cur_nv["points"])

    f1 = lambda v: f"{v:+.2f}%"
    print(f"== {run_dir.name} 满仓单调 vs 现行建议层（费前 / 费后 {args.fee}% 双边） ==")
    print(f"满仓单调   : 费前 {f1(st['total_gross_pct'])} · 费后 {f1(st['total_pct'])} · "
          f"年化 {f1(st['ann_pct'])} · 回撤 {f1(st['max_dd_pct'])} · 敞口 {st['exposure']}%")
    print(f"S1 环境缩仓: 费前 {f1(st_s1['total_gross_pct'])} · 费后 {f1(st_s1['total_pct'])} · "
          f"回撤 {f1(st_s1['max_dd_pct'])}")
    print(f"Naive 无纪律: 费前 {f1(st_nv['total_gross_pct'])} · 费后 {f1(st_nv['total_pct'])} · "
          f"回撤 {f1(st_nv['max_dd_pct'])}")
    print(f"-> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
