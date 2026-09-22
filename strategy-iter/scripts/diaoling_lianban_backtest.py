# -*- coding: utf-8 -*-
"""满仓单吊·只做连板变体资金曲线回测（2026-09-20）：选股范围收窄 + 集中持仓的极端变体。

用户设问：「如果满仓单吊只做连板呢，其收益率如何」。本脚本在现行策略（C7 冻结选股
+ 执行层 MF_BUY，runs/c12_window_C7）的入选样本内回答：

策略定义（单吊连板 = M_LB1）：
- 选股池：现行涨停组入选样本中，连板 ≥2 板的子集（ladder 因子档位反推——
  M3 冻结 ladder_tiers={1:0.55, 2:0.9, 3:1.0, 4:1.0}，0.9=2板 / 1.0=≥3板，
  与 reason 文本「N板」交叉核对；不重新选股）。
- 执行层：与现行完全一致（backtest_capital.realtime 单一来源——开盘窗 [0,+5]%
  买入，低开不接/高开放弃；risk_low=-5% 破位离场；否则 T+1 收盘离场）。
- 资金管理：满仓单吊——当日候选中按 pick_score 取第 1 名，全部资金买入一只；
  无候选/未触发日空仓。T+1 收盘/止损离场，日度复利。

对照曲线（量化每个自由度的贡献）：
- 单吊·次高分：同日候选中取第 2 名（无则空仓）——度量「单吊选哪只」的路径敏感性；
- 连板等权满仓：当日全部连板候选等权（不单吊）——度量「单吊集中度」的贡献；
- 单吊·最高分 Naive：入选即买（无窗口无止损）——度量执行层纪律在该子集的价值。

口径：日度复利；年化 244 交易日；费前/费后（双边 0.15%）双报；无未来函数
（候选与排序均来自 T 日复盘产出，执行按 T+1 盘中信息顺序）。

诚实披露：单吊是**单路径**模拟，同日多候选时「选第 1 名」是唯一自由度，结果对
该选择高度敏感（次高分对照即为敏感性上界参照）；连板子集样本量小，统计意义弱；
满仓单吊还有模拟未覆盖的现实约束（单票容量/流动性/一字板买不进）。

用法：
    python strategy-iter/scripts/diaoling_lianban_backtest.py --run runs/c12_window_C7
输出：<run>/diaoling_report.md + <run>/diaoling_curve.csv；纯函数可直接单测。
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import sys
from pathlib import Path

TRADING_DAYS_PER_YEAR = 244
FEE = 0.15  # 双边成本（%），与 exp_hold_sizing / mf_mono_backtest 同口径

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backtest_capital import trade_pnl  # noqa: E402
from scripts.mf_mono_backtest import curve_from_daily, stats_block, monthly_table  # noqa: E402

# M3 冻结 ladder_tiers 的档位分 → 连板数（唯一映射；0.55=1板被排除）
LADDER_TIER_TO_N = {0.55: 1, 0.9: 2, 1.0: 3}   # 1.0 档 = ≥3 板（含 4 板档合并）


def naive_pnl(r: dict) -> float | None:
    """Naive 口径单票收益：入选即买（T+1 开盘）、收盘全卖，无窗口无止损。"""
    o, c = r["o"], r["c"]
    if o is None or c is None:
        return None
    return (1 + c / 100.0) / (1 + o / 100.0) - 1


def load_rows(run_dir: Path) -> list[dict]:
    """validation.csv 全量列 → [row dict]（含 pick_score；backtest_capital 的
    load_validation 不带该列，这里只做超集读取，不重新选股）。"""
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
                "score": num("pick_score"),
                "o": num("open_ret"), "h": num("high_ret"), "l": num("low_ret"),
                "c": num("close_ret"),
            })
    return out


def load_ladder(run_dir: Path) -> dict[tuple[str, str], int]:
    """picks.csv factors.ladder 档位分 → (T, thscode) → 连板数（2/3+；1板与缺失排除）。"""
    fp = Path(run_dir) / "picks.csv"
    out = {}
    with open(fp, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["group"] != "lu":
                continue
            try:
                fac = ast.literal_eval(r["factors"]) if r.get("factors") else {}
            except (ValueError, SyntaxError):
                continue
            tier = fac.get("ladder")
            n = LADDER_TIER_TO_N.get(tier if tier is None else round(float(tier), 3))
            if n and n >= 2:
                out[(r["T"], r["thscode"])] = n
    return out


def lianban_daily(rows: list[dict], ladder: dict, rank: int, mode: str = "realtime") -> list[dict]:
    """单吊/等权的逐日组合（全窗口口径：无候选日也入曲线、收益 0，与满仓单调可比）。
    rank=1 取当日最高分候选，rank=2 取次高（无则空仓）；
    rank=0 表示当日全部候选等权。mode：realtime=执行层纪律 / naive=入选即买。"""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        if r["group"] != "lu" or (r["T"], r["thscode"]) not in ladder:
            continue
        by_day.setdefault(r["T1"], []).append(r)
    daily = []
    for t1 in sorted({r["T1"] for r in rows}):     # 全部 T+1 交易日，含无候选日
        cands = sorted(by_day.get(t1, []),
                       key=lambda r: (-(r["score"] or 0.0), r["thscode"]))
        if rank > 0:
            picks = cands[rank - 1:rank]          # 单吊：第 rank 名一只
        else:
            picks = cands                          # 等权：全部候选
        pnls = []
        for r in picks:
            p = naive_pnl(r) if mode == "naive" else trade_pnl(r, mode=mode)
            if p is not None:
                pnls.append((r, p))
        day = sum(p for _r, p in pnls) / len(pnls) if pnls else 0.0
        daily.append({"T1": t1, "n_pick": len(picks), "n_trade": len(pnls),
                      "day_ret": day,
                      "trades": [(r["thscode"], r["name"], ladder[(r["T"], r["thscode"])],
                                  round(p * 100, 2)) for r, p in pnls]})
    return daily


def pool_stats(rows: list[dict], ladder: dict) -> dict:
    lb = [r for r in rows if r["group"] == "lu" and (r["T"], r["thscode"]) in ladder]
    days = {r["T1"] for r in lb}
    dist: dict[int, int] = {}
    for r in lb:
        n = ladder[(r["T"], r["thscode"])]
        key = 2 if n == 2 else 3
        dist[key] = dist.get(key, 0) + 1
    return {"picks": len(lb), "days": len(days), "dist": dist}


def render_report(run_name: str, window: str, pool: dict, st: dict, st2: dict,
                  stw: dict, stn: dict, monthly: list[dict], trades: list,
                  fee: float) -> str:
    f1 = lambda v: "-" if v is None else f"{v:+.2f}%"
    lines = [f"# {run_name} 满仓单吊·只做连板 变体资金曲线回测", "",
             f"验证区间 {window}（{st['days']} 个 T+1 交易日）。策略定义与口径见脚本头注："
             "选股池 = 现行涨停组入选中连板 ≥2 板的子集"
             f"（{pool['picks']} 笔 / {pool['days']} 天有候选，"
             f"其中 2 板 {pool['dist'].get(2, 0)} 笔、≥3 板 {pool['dist'].get(3, 0)} 笔）；"
             "执行层与现行一致；资金管理 = 每日按 pick_score 单吊一只、满仓。", "",
             "## 主答案：满仓单吊·最高分（M_LB1）", "",
             "```text",
             f"费前 累计 {f1(st['total_gross_pct'])} · 年化 {f1(st['ann_gross_pct'])} · 回撤 {f1(st['max_dd_gross_pct'])}",
             f"费后 累计 {f1(st['total_pct'])}（{st['traded_days']}/{st['days']} 日有持仓，敞口 {st['exposure']}%）",
             f"     年化 {f1(st['ann_pct'])} · 最大回撤 {f1(st['max_dd_pct'])} · Calmar {st['calmar']}",
             f"     有持仓日 胜率 {st['day_win']}%",
             "```", "",
             "## 对照：单吊·次高分（同日第 2 名；度量「选哪只」的路径敏感性）", "",
             "```text",
             f"费前 累计 {f1(st2['total_gross_pct'])} · 费后 累计 {f1(st2['total_pct'])} · "
             f"回撤 {f1(st2['max_dd_pct'])} · Calmar {st2['calmar']}",
             "```", "",
             "## 对照：连板等权满仓（不单吊，当日全部连板候选等权）", "",
             "```text",
             f"费前 累计 {f1(stw['total_gross_pct'])} · 费后 累计 {f1(stw['total_pct'])} · "
             f"回撤 {f1(stw['max_dd_pct'])} · Calmar {stw['calmar']}",
             "```", "",
             "## 对照：单吊·最高分 Naive（入选即买、收盘全卖，无窗口无止损）", "",
             "```text",
             f"费前 累计 {f1(stn['total_gross_pct'])} · 费后 累计 {f1(stn['total_pct'])} · "
             f"回撤 {f1(stn['max_dd_pct'])}",
             "```", "",
             f"## 月度（单吊·最高分，费后 {fee}% 双边）", "",
             "| 月 | 月收益 | 持仓日/交易日 |", "|---|---|---|"]
    for m in monthly:
        lines.append(f"| {m['month']} | {f1(m['ret_pct'])} | {m['full_days']}/{m['days']} |")
    lines += ["", "## 单笔极值（主口径）", "", "```text"]
    big_win = sorted(trades, key=lambda t: -t[3])[:5]
    big_loss = sorted(trades, key=lambda t: t[3])[:5]
    lines.append("最大 5 笔: " + " | ".join(f"{n}({b}板){p:+.1f}%" for _c, n, b, p in big_win))
    lines.append("最小 5 笔: " + " | ".join(f"{n}({b}板){p:+.1f}%" for _c, n, b, p in big_loss))
    lines += ["```", "",
              "> 口径：连板数由 picks.csv factors.ladder 档位分按 M3 冻结 ladder_tiers 反推"
              "（0.9=2板、1.0=≥3板）。执行近似沿用 backtest_capital 头注（开盘窗 [0,+5]%、"
              "risk_low=-5% 破位离场、否则 T+1 收盘离场）；费后为双边 "
              f"{fee}%。单吊为单路径模拟，「选第 1 名」是唯一自由度，次高分对照即敏感性参照；"
              "样本量小、统计意义弱；单票容量/一字板买不进等现实约束未建模。"
              "本回测为规则化模拟，非投资建议。"]
    return "\n".join(lines)


def write_curve_csv(path: Path, pts1: list[dict], pts2: list[dict],
                    ptsw: list[dict], ptsn: list[dict]):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["T1", "top_day_gross_pct", "top_day_net_pct", "top_equity",
                "second_equity", "equalw_equity", "top_naive_equity"])
    for a, b, c, d in zip(pts1, pts2, ptsw, ptsn):
        w.writerow([a["T1"], round(a["day_gross_pct"], 4), round(a["day_net_pct"], 4),
                    round(a["equity"], 6), round(b["equity"], 6),
                    round(c["equity"], 6), round(d["equity"], 6)])
    path.write_text(buf.getvalue(), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="满仓单吊·只做连板 变体资金曲线回测")
    ap.add_argument("--run", default="runs/c12_window_C7",
                    help="engine run 目录（含 validation.csv + picks.csv）")
    ap.add_argument("--fee", type=float, default=FEE, help="双边成本%%（默认 0.15）")
    args = ap.parse_args(argv)
    base = Path(__file__).resolve().parent.parent
    cand = Path(args.run)
    run_dir = cand if cand.is_absolute() else (cand if cand.exists() else base / args.run)
    if not (run_dir / "validation.csv").is_file() or not (run_dir / "picks.csv").is_file():
        print(f"找不到 {run_dir}/validation.csv 或 picks.csv")
        return 1
    rows = load_rows(run_dir)
    ladder = load_ladder(run_dir)
    if not rows or not ladder:
        print("validation.csv / picks.csv 为空或无连板候选")
        return 1
    pool = pool_stats(rows, ladder)

    daily1 = lianban_daily(rows, ladder, rank=1, mode="realtime")
    daily2 = lianban_daily(rows, ladder, rank=2, mode="realtime")
    dailyw = lianban_daily(rows, ladder, rank=0, mode="realtime")
    dailyn = lianban_daily(rows, ladder, rank=1, mode="naive")

    def curves(daily):
        net = curve_from_daily(daily, size_fn=None, fee=args.fee)
        gross = curve_from_daily(daily, size_fn=None, fee=0.0)
        return net, gross

    cur1, cur1_g = curves(daily1)
    cur2, cur2_g = curves(daily2)
    curw, curw_g = curves(dailyw)
    curn, curn_g = curves(dailyn)

    st = stats_block(cur1, daily1, args.fee, "单吊·最高分", gross=cur1_g)
    st2 = stats_block(cur2, daily2, args.fee, "单吊·次高分", gross=cur2_g)
    stw = stats_block(curw, dailyw, args.fee, "连板等权满仓", gross=curw_g)
    stn = stats_block(curn, dailyn, args.fee, "单吊·最高分Naive", gross=curn_g)
    monthly = monthly_table(cur1["points"])
    trades = [t for d in daily1 for t in d["trades"]]
    window = f"{rows[0]['T1']} → {max(r['T1'] for r in rows)}"

    report = render_report(run_dir.name, window, pool, st, st2, stw, stn,
                           monthly, trades, args.fee)
    out_md = run_dir / "diaoling_report.md"
    out_md.write_text(report, encoding="utf-8")
    write_curve_csv(run_dir / "diaoling_curve.csv",
                    cur1["points"], cur2["points"], curw["points"], curn["points"])

    f1 = lambda v: f"{v:+.2f}%"
    print(f"== {run_dir.name} 满仓单吊·只做连板（连板候选 {pool['picks']} 笔/{pool['days']} 天） ==")
    print(f"单吊·最高分 : 费前 {f1(st['total_gross_pct'])} · 费后 {f1(st['total_pct'])} · "
          f"年化 {f1(st['ann_pct'])} · 回撤 {f1(st['max_dd_pct'])} · 敞口 {st['exposure']}%")
    print(f"单吊·次高分 : 费前 {f1(st2['total_gross_pct'])} · 费后 {f1(st2['total_pct'])} · "
          f"回撤 {f1(st2['max_dd_pct'])}")
    print(f"连板等权满仓: 费前 {f1(stw['total_gross_pct'])} · 费后 {f1(stw['total_pct'])} · "
          f"回撤 {f1(stw['max_dd_pct'])}")
    print(f"单吊Naive   : 费前 {f1(stn['total_gross_pct'])} · 费后 {f1(stn['total_pct'])} · "
          f"回撤 {f1(stn['max_dd_pct'])}")
    print(f"-> {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
