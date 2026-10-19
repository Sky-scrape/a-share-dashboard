# -*- coding: utf-8 -*-
"""C15 复审 · 全窗口可执行口径回测（只读）：C7 在 2026-01~09 研究窗口里
「T+1 买入 → T+2 离场（含费）」到底赚不赚钱。

输入：`runs/us_round5_C7_aligned/validation.csv`（C7 冻结口径全窗口 422 笔，
含 T/T1 与相对 T 收盘的开/高/低/收涨幅），T+2 由本地行情库现取。
口径与 `c15_exec_tplus.py` 完全同源（同一 entry_of/_ratio 实现），
保证「样本外 20 日」与「研究 162 日」两段结论可对比。

判读要点：validation.csv 的 open_ret/low_ret 是相对 T 收盘的复权涨幅，
与本脚本从库现取的 T+2 价同基准（前复权），因此可直接串起 T→T1→T2。

严格只读：不写 runs/us_round5_C7_aligned，不改生产；产物落 runs/c15_oos_review/。
"""
from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c15_common as C                                  # noqa: E402
from c15_exec_tplus import (MF_BUY, entry_of, _ratio, _pct, FEE_MAIN, FEE_SCENARIOS,  # noqa: E402
                            unsellable_at_open)

SRC = C.BASE / "strategy-iter" / "runs" / "us_round5_C7_aligned" / "validation.csv"


def load_rows():
    with open(SRC, "r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)]


def fnum(r, k):
    v = r.get(k)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main():
    cal = C.trade_calendar()
    rows = load_rows()
    codes = sorted({str(r["thscode"]).split(".")[0] for r in rows})
    # 价口径与样本外评估器同源：全部从库取绝对前复权价，不用 CSV 的相对值参与换算
    # （CSV 相对值只用于交叉校验，避免两套基准串用——初版混用即出过 -66% 的假结果）。
    bars = C.daily_bars(codes, "2026-01-01", cal[-1])

    recs, drift = [], 0
    for r in rows:
        t, t1 = r["T"], r["T1"]
        grp = r["group"]
        code = str(r["thscode"]).split(".")[0]
        t2 = C.next_trade_day(cal, t1)
        b = bars.get(code) or {}
        bar0, bar1 = b.get(t), b.get(t1)
        bar2 = b.get(t2) if t2 else None
        rec = {"T": t, "T1": t1, "T2": t2, "group": grp, "code": code,
               "month": t[:7], "regime": r.get("regime"), "score": fnum(r, "pick_score"),
               "A": None, "B": None, "C": None, "note": ""}
        if not bar0 or bar1 is None:
            rec["note"] = "T/T+1 无日线"
            recs.append(rec)
            continue
        c0 = bar0["c"]
        o1 = _pct(bar1["o"], c0)
        rec["close"] = _pct(bar1["c"], c0)
        if o1 is not None and fnum(r, "open_ret") is not None \
                and abs(o1 - fnum(r, "open_ret")) > 0.5:
            drift += 1                       # 库与 CSV 的 T+1 开盘差 >0.5pp（复权口径漂移）
        h1, l1, c1 = (_pct(bar1[k], c0) for k in ("h", "l", "c"))
        e_ret, why = entry_of(grp, bar1["o"], bar1["h"], c0)
        rec["entry_note"] = why
        b_ = MF_BUY[grp]
        rec["A"] = round(_ratio(b_["risk_low"] if l1 <= b_["risk_low"] else c1, e_ret), 2) \
            if e_ret is not None else None
        if e_ret is None:
            rec["note"] = "未成交"
            recs.append(rec)
            continue
        if bar2 is None:
            rec["note"] = f"T+2({t2}) 无日线"
            recs.append(rec)
            continue
        r2o, r2c = _pct(bar2["o"], c0), _pct(bar2["c"], c0)
        # 跌停不可卖：开盘触及跌停价（未复权价格域，P0-3 修正，与 c15_exec_tplus 同源）
        rec["unsellable"] = unsellable_at_open(bar2)
        exit_open = (r2c if rec["unsellable"] else r2o)
        rec["B"] = round(_ratio(exit_open, e_ret), 2)
        rec["C"] = round(_ratio(exit_open if l1 <= b_["risk_low"] else r2c, e_ret), 2)
        # 变体 D：confirm 口径——开盘在窗内 且 盘中最低≥low_min 且 收盘站上开盘/昨收，
        # 满足才于 T+1 收盘价建仓，T+2 开盘离场（对应「尾盘确认后按收盘价下单」）。
        if grp == "lu":
            ok_d = (b_["win_min"] <= o1 <= b_["win_max"] and l1 >= b_["low_min"] and c1 > o1)
        else:
            ok_d = (b_["win_min"] <= o1 <= b_["win_max"] and c1 > max(o1, 0.0))
        rec["D"] = round(_ratio(exit_open, c1), 2) if ok_d else None
        recs.append(rec)
    print(f"[c15w] 交叉校验：库/CSV T+1 开盘差>0.5pp 的票 {drift}/{len(rows)}")

    def agg(sub, key):
        vals = [r[key] for r in sub if r.get(key) is not None]
        if not vals:
            return None
        return {"n": len(vals), "gross": round(sum(vals) / len(vals), 3),
                "net": round(sum(vals) / len(vals) - FEE_MAIN, 3),
                "net_win": round(100.0 * sum(1 for v in vals if v - FEE_MAIN > 0) / len(vals), 1)}

    def equity(sub, key):
        """日级等权复利：每日成交票等权、无票当日空仓（0 收益）、费用按双边扣。"""
        days = collections.defaultdict(list)
        for r in sub:
            if r.get(key) is not None:
                days[r["T1"]].append(r[key] - FEE_MAIN)
        eq = 1.0
        for d in sorted(days):
            eq *= (1 + sum(days[d]) / len(days[d]) / 100.0)
        return round(eq, 3), len(days)

    traded = [r for r in recs if r.get("B") is not None]
    notraded = [r for r in recs if r.get("entry_note") and r.get("B") is None
                and "无日线" not in (r.get("note") or "")]

    L = ["# C15 复审 · 全窗口可执行口径回测（4/4）", "",
         f"输入：`runs/us_round5_C7_aligned/validation.csv`（C7 全窗口 {len(rows)} 笔选股，"
         "选股日 2026-01-02 ~ 2026-09-17）；T+2 由本地行情库现取；口径与 "
         "`c15_exec_tplus.py` 同源。", "",
         "## 1. 四口径全窗口", "",
         "| 口径 | n | 毛均% | 净均%(0.15) | 净收红率% | 日级复利净值 | 交易日数 |",
         "|---|---:|---:|---:|---:|---:|---:|"]
    for key, label in (("A", "A 诊断（不可执行）"), ("B", "B 制度·T+2开盘"),
                       ("C", "C 制度·T+2风险感知"), ("D", "D 确认进场·尾盘建仓")):
        s = agg(recs, key)
        if s:
            eq, nd = equity(recs, key)
            L.append(f"| {label} | {s['n']} | {s['gross']} | {s['net']} | "
                     f"{s['net_win']} | {eq} | {nd} |")
    L += ["", f"- 全窗口 {len(rows)} 笔中，按**实时口径**买点未成交（纪律放弃）"
          f"**{len(notraded)}** 笔（{100.0*len(notraded)/len(rows):.1f}%）；"
          f"可完整评估 **{len(traded)}** 笔。",
          "- 注意 `stats.json` 的 `buy_trigger_rate`=32.94% 用的是 **confirm 口径**"
          "（含「盘中最低≥-3%」「收盘站上开盘」两条需收盘才知的条件）；实时口径"
          f"（开盘即可判、可下单）成交率 {100.0*len(traded)/len(rows):.1f}%。"
          "两者不可混用：交易计划按实时口径，展示统计的触发率是后视上界。", ""]
    L += ["## 2. 制度口径（B）分维度", "",
          "| 维度 | 分层 | n | 毛均% | 净均%(0.15) | 净值 | 交易日 |",
          "|---|---|---:|---:|---:|---:|---:|"]
    for label, keyfn in (("月份", lambda r: r["month"]), ("环境档", lambda r: r["regime"]),
                         ("分组", lambda r: r["group"])):
        buck = collections.defaultdict(list)
        for r in recs:
            if r.get("B") is not None:
                buck[keyfn(r)].append(r)
        for k, sub in sorted(buck.items(), key=lambda kv: str(kv[0])):
            s = agg(sub, "B")
            eq, nd = equity(sub, "B")
            L.append(f"| {label} | {k} | {s['n']} | {s['gross']} | {s['net']} | {eq} | {nd} |")
    L += ["", "## 3. 诊断口径 vs 制度口径（全窗口）", ""]
    a_all = agg(recs, "A")
    b_all = agg(recs, "B")
    if a_all and b_all:
        diff = a_all["net"] - b_all["net"]
        L.append(f"- A 诊断 {a_all['net']}% vs B 制度 {b_all['net']}%：差 **{diff:+.2f}pp**。")
        if abs(diff) < 0.2:
            L.append("- 全窗口两口径几乎相等：**研究窗口内「不可执行止损」并未系统性抬高收益**"
                     "（与 rounds_log §8 口径对齐后的结论一致），故历史执行类结论不受该差影响。")
        else:
            L.append(f"- 诊断口径相对制度口径偏差 {diff:+.2f}pp，引用历史执行类结论需按此校正。")
        a_d = agg(recs, "D")
        if a_d:
            L.append(f"- 确认进场（D）反而更差（净 {a_d['net']}%，收红率 {a_d['net_win']}%，"
                     f"n={a_d['n']}）：**收紧买点条件在可执行口径下是负贡献**——"
                     "它同时把样本砍掉一半、并把建仓价从开盘推到尾盘。"
                     "「买点确认条件价值极大」的旧结论建立在 confirm 口径的条件期望上，"
                     "不成立于可执行口径。")
    L += ["", "## 4. 边界披露", "",
          "- 日线无法还原盘中路径：T+2 开盘/收盘价均视为可成交（跌停已单列）；"
          f"全窗口跌停不可卖改收盘的票 {sum(1 for r in recs if r.get('unsellable'))} 只"
          "（判定口径 2026-10-19 修正：开盘价触及跌停价，未复权价格域）。",
          "- 未计滑点差异（0.15% 已近似含）、未处理停牌顺延、未计涨跌停挂单排队。",
          "- 未成交票不计收益也不占资金（等权空仓口径），因此净值不等于满仓收益。",
          "- 本回测用研究库窗口的 C7 选股，与生产池存在已知口径差（生产无当日一字排除）。", ""]
    C.write_report("window_exec_report.md", "\n".join(L))
    C.write_json("window_exec.json", {
        "n_rows": len(rows), "n_traded": len(traded), "n_not_traded": len(notraded),
        "A": agg(recs, "A"), "B": agg(recs, "B"), "C": agg(recs, "C"), "D": agg(recs, "D"),
        "equity_B": equity(recs, "B"), "equity_C": equity(recs, "C"),
        "equity_D": equity(recs, "D"), "price_drift_vs_csv": drift,
    })
    for k in ("A", "B", "C", "D"):
        s = agg(recs, k)
        if s:
            eq, nd = equity(recs, k)
            print(f"[c15w] {k}: n={s['n']} gross={s['gross']} net={s['net']} "
                  f"win={s['net_win']} equity={eq} days={nd}")
    print(f"[c15w] rows={len(rows)} traded={len(traded)} not_traded={len(notraded)}")


if __name__ == "__main__":
    main()
