# -*- coding: utf-8 -*-
"""C15 复审 · 第三步：A 股交易制度下的可执行收益评估（只读）。

动机：生产与研究的「执行近似」允许 T+1 买入当日按 risk_low 止损离场
（`spec_validate._exec_pnl_realtime` / `backtest_capital.trade_pnl` 同一实现），
但 A 股 T+1 制度下当日买入不可卖出——该止损实际不可执行。本脚本给出在
「T+1 按买点成交 → T+1 收盘不可卖 → T+2 才可离场」约束下的费后收益，
并量化它与现行诊断口径的差。

口径（三套并列，互不替代）：
  A 诊断口径（现行）：T+1 盘中触 risk_low 即按止损价离场（不可执行的乐观代理）
  B 制度口径·开盘离场：T+1 买 → T+2 开盘卖（最保守，开盘价可得）
  C 制度口径·风险感知：T+1 触过 risk_low 则 T+2 开盘卖，否则 T+2 收盘卖

费用：双边 0.15%（佣金+滑点近似，与 scripts/mf_mono_backtest.py 同源常量），
并列 0.00% / 0.30% 情景；滑点不另计（已在 0.15 内近似）。
跌停不可卖：T+2 开盘价触及跌停价（未复权 prev_close×0.9，主板 10%）时
无法开盘成交，改按 T+2 收盘价，并单列计数。

严格只读：不写 data/recap，不改生产代码；产物落 runs/c15_oos_review/。
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend" / "recap"))

import c15_common as C      # noqa: E402
import c15_oos_audit as A   # noqa: E402
from execution_layer import MF_BUY          # 执行层单一来源（不许复制常量）  # noqa: E402
from spec_validate import _exec_pnl_realtime  # 现行诊断口径（对照用）        # noqa: E402

FEE_MAIN = 0.15        # 双边成本（%），与 mf_mono_backtest.FEE 同源
FEE_SCENARIOS = (0.0, 0.15, 0.30)
LIMIT_PCT = 0.10       # 主板涨跌幅（%）；ST 已在选池负面清单排除
EPS = 0.005


def _pct(a, base):
    """a 相对 base 的涨幅（%）。"""
    if a is None or not base:
        return None
    return (a / base - 1.0) * 100.0


def _ratio(exit_ret, entry_ret):
    """按（昨收相对）涨幅口径换算成相对建仓价的收益（%）——与现行诊断同算法。"""
    return ((1 + exit_ret / 100.0) / (1 + entry_ret / 100.0) - 1) * 100.0


def entry_of(group, o, h, c_prev):
    """买点判定（与 MF_BUY 窗口一致）：返回 (entry_ret, 说明) 或 (None, 原因)。"""
    b = MF_BUY[group]
    ret_o = _pct(o, c_prev)
    if ret_o is None:
        return None, "无开盘价"
    if not (b["win_min"] <= ret_o <= b["win_max"]):
        return None, "开盘超窗放弃"
    if group == "nlu":
        limit = max(o, c_prev)
        if o >= c_prev:
            return ret_o, "开盘即成交"
        if h is not None and h >= limit:
            return 0.0, "回踩昨收成交"
        return None, "限价未触及"
    return ret_o, "开盘市价成交"


def evaluate(rows, bars_all):
    """逐票评估：返回记录列表（含三套口径与跌幅信息）。"""
    out = []
    for r in rows:
        code = str(r.get("code") or "")
        grp = r.get("group")
        d8 = r.get("pool_date")
        if grp not in MF_BUY or not d8:
            continue
        cal = bars_all["cal"]
        t1 = C.next_trade_day(cal, C.iso(d8))
        t2 = C.next_trade_day(cal, t1) if t1 else None
        b = (bars_all["bars"].get(code) or {})
        bar0, bar1 = b.get(C.iso(d8)), (b.get(t1) if t1 else None)
        bar2 = b.get(t2) if t2 else None
        rec = {"validation_date": r["validation_date"], "pool_date": d8, "code": code,
               "name": r.get("name"), "group": grp, "env": r.get("env"),
               "score": r.get("pick_score"), "close": r.get("close"),
               "t1": t1, "t2": t2, "diag": None, "A": None, "B": None, "C": None,
               "note": ""}
        if not bar0 or bar1 is None:
            rec["note"] = "T+1 无日线（停牌/链断）"
            out.append(rec)
            continue
        e_ret, why = entry_of(grp, bar1["o"], bar1["h"], bar0["c"])
        rec["entry_note"] = why
        # 现行诊断口径（对照）：不需要 T+2
        rec["diag"] = _exec_pnl_realtime(grp, _pct(bar1["o"], bar0["c"]),
                                         _pct(bar1["h"], bar0["c"]),
                                         _pct(bar1["l"], bar0["c"]),
                                         _pct(bar1["c"], bar0["c"]))
        if e_ret is None:
            rec["note"] = "未成交"
            out.append(rec)
            continue
        r1o, r1h = _pct(bar1["o"], bar0["c"]), _pct(bar1["h"], bar0["c"])
        r1l, r1c = _pct(bar1["l"], bar0["c"]), _pct(bar1["c"], bar0["c"])
        b_ = MF_BUY[grp]
        rec["risk_low_hit"] = r1l is not None and r1l <= b_["risk_low"]
        rec["risk_close_hit"] = r1c is not None and r1c <= b_["risk_close"]
        rec["mark_t1"] = round(_ratio(r1c, e_ret), 2)      # T+1 收盘浮盈/浮亏（不可卖）
        if bar2 is None:
            rec["note"] = "T+2 日线未到（今天尚未收盘）"
            out.append(rec)
            continue
        r2o, r2c = _pct(bar2["o"], bar0["c"]), _pct(bar2["c"], bar0["c"])
        # 跌停不可卖（用未复权价判：复权价无法判跌停）
        rc, rpc = bar2.get("raw_c"), bar2.get("raw_pc")
        rec["unsellable_open"] = bool(rc and rpc and bar2["o"] is not None
                                      and abs(bar2["o"] - round(rpc, 2)) < 1e-6
                                      and abs(rc - round(rpc * (1 - LIMIT_PCT), 2)) < EPS)
        exit_open = r2c if rec["unsellable_open"] else r2o      # 跌停则只能按收盘离场
        rec["A"] = round(_ratio(b_["risk_low"] if rec["risk_low_hit"] else r1c, e_ret), 2)
        rec["B"] = round(_ratio(exit_open, e_ret), 2)
        rec["C"] = round(_ratio(exit_open if rec["risk_low_hit"] else r2c, e_ret), 2)
        rec["exit_kind"] = ("跌停改收盘" if rec["unsellable_open"] else "开盘")
        out.append(rec)
    return out


def gross(v):
    """去掉费用前的合并口径（元）：记录里是百分比，费用按双边线性扣。"""
    return v


def agg(recs, key):
    """按口径聚合：n_exec / 平均毛收益 / 各费用情景平均净收益 / 净收红率。"""
    vals = [r[key] for r in recs if r.get(key) is not None]
    if not vals:
        return {"n": 0}
    res = {"n": len(vals),
           "gross_avg": round(sum(vals) / len(vals), 2),
           "gross_win": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
           "net": {}}
    for fee in FEE_SCENARIOS:
        net = [v - fee for v in vals]
        res["net"][fee] = {
            "avg": round(sum(net) / len(net), 2),
            "win": round(100.0 * sum(1 for v in net if v > 0) / len(net), 1),
        }
    return res


def main():
    cal = C.trade_calendar()
    track = C.load_track()
    recs_audit, dup, missing = A.audit_records(track, cal)
    rows = A.enrich(A.clean_rows(track, recs_audit))
    codes = sorted({str(r.get("code")) for r in rows if r.get("code")})
    d0 = min(r["pool_date"] for r in rows)
    bars_all = {"cal": cal, "bars": C.daily_bars(codes, C.iso(d0), cal[-1])}

    ev = evaluate(rows, bars_all)
    traded = [r for r in ev if r.get("B") is not None]
    noT2 = [r for r in ev if r.get("entry_note") and "T+2" in (r.get("note") or "")]
    notraded = [r for r in ev if r.get("entry_note") and r.get("B") is None
                and "T+2" not in (r.get("note") or "")]

    L = ["# C15 复审 · A 股可执行口径收益（3/3）", "",
         "样本 = 修正口径 C7 票（日期严格匹配）。三套口径：", "",
         "- **A 诊断口径（现行）**：T+1 盘中触 risk_low 即止损离场——"
         "A 股 T+1 制度下当日买入不可卖，**该止损不可执行**，只作乐观代理。",
         "- **B 制度口径·开盘离场**：T+1 按买点成交 → T+1 收盘持有（不可卖）→ T+2 开盘卖出。",
         "- **C 制度口径·风险感知**：T+1 触过 risk_low 则 T+2 开盘卖，否则 T+2 收盘卖。",
         "",
         f"费用：双边 {FEE_MAIN}%（佣金+滑点近似）为主情景，并列 0.00% / 0.30%；"
         "跌停不可卖时改按 T+2 收盘并计数。", "",
         "## 1. 总览", "",
         f"- 评估票数：{len(ev)}；其中买点未成交（按纪律放弃）：**{len(notraded)}** 只。",
         f"- 已成交但 T+2 日线未到（近两日）：**{len(noT2)}** 只，未计入收益。",
         f"- 可完整评估：**{len(traded)}** 只。", ""]
    for key, label in (("A", "A 诊断口径（现行，不可执行）"),
                       ("B", "B 制度口径·T+2 开盘离场"),
                       ("C", "C 制度口径·T+2 风险感知离场")):
        s = agg(ev, key)
        if s.get("n"):
            L.append(f"- {label}：n={s['n']}，毛均 {s['gross_avg']}%，"
                     f"毛收红率 {s['gross_win']}%，净均（{FEE_MAIN}%）"
                     f"{s['net'][FEE_MAIN]['avg']}%，净收红率 {s['net'][FEE_MAIN]['win']}%。")
    L += ["", "## 2. 分口径 × 分组", "",
          "| 口径 | 分组 | n | 毛均% | 净均%(0.15) | 净收红率% |", "|---|---|---:|---:|---:|---:|"]
    for key, label in (("A", "A 诊断"), ("B", "B T+2开盘"), ("C", "C T+2风险感知")):
        for grp in ("lu", "nlu"):
            sub = [r for r in ev if r.get("group") == grp]
            s = agg(sub, key)
            if s.get("n"):
                L.append(f"| {label} | {grp} | {s['n']} | {s['gross_avg']} | "
                         f"{s['net'][FEE_MAIN]['avg']} | {s['net'][FEE_MAIN]['win']} |")
    L += ["", "## 3. 费用情景（B 制度口径·开盘离场）", "",
          "| 双边费用% | 平均净收益% | 净收红率% |", "|---:|---:|---:|"]
    sB = agg(ev, "B")
    for fee in FEE_SCENARIOS:
        L.append(f"| {fee:.2f} | {sB['net'][fee]['avg']} | {sB['net'][fee]['win']} |")
    L += ["", "**B 制度口径 × 月份 / 环境档 / 分组**（判断负收益是结构性还是某段行情）", "",
          "| 维度 | 分层 | n | 毛均% | 净均%(0.15) |", "|---|---|---:|---:|---:|"]
    for label, keyfn in (("月份", lambda r: "2026-" + r["validation_date"][4:6]),
                         ("环境档", lambda r: r.get("env")),
                         ("分组", lambda r: r.get("group"))):
        buckets = collections.defaultdict(list)
        for r in ev:
            if r.get("B") is not None:
                buckets[keyfn(r)].append(r)
        for k, sub in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            s = agg(sub, "B")
            L.append(f"| {label} | {k} | {s['n']} | {s['gross_avg']} | "
                     f"{s['net'][FEE_MAIN]['avg']} |")
    L += ["", "## 4. 现行诊断口径 vs 制度口径（差 = 不可执行止损的乐观成分）", ""]
    pair = [(r["diag"], r["B"]) for r in ev if r.get("diag") is not None and r.get("B") is not None]
    if pair:
        da = sum(p[0] for p in pair) / len(pair)
        db = sum(p[1] for p in pair) / len(pair)
        L.append(f"- 同一 {len(pair)} 只票：A 诊断平均 {da:.2f}%，B 制度平均 {db:.2f}%，"
                 f"差 **{da - db:+.2f}pp**（正差 = 诊断口径系统性高估）。")
        both_neg = sum(1 for a, b in pair if a > 0 >= b)
        L.append(f"- 诊断口径显示盈利、制度口径实为亏损的票：**{both_neg}** 只。")
        L.append(f"- 其中 T+1 触过 risk_low 的票："
                 f"{sum(1 for r in ev if r.get('risk_low_hit'))} 只，"
                 f"这些票在制度口径下只能次日离场，风险暴露至少多留一夜。")
    uns = [r for r in ev if r.get("unsellable_open")]
    L.append(f"- T+2 开盘跌停不可卖、改按收盘离场的票：{len(uns)} 只。")

    # 收益差分解：从「选股质量」到「制度口径净收益」的每一段损耗
    L += ["", "## 5. 缺口分解：选股质量 → 实际净收益", ""]
    allc = [r["close"] for r in ev if r.get("close") is not None]
    tr_c = [r["close"] for r in traded]
    mk = [r["mark_t1"] for r in traded if r.get("mark_t1") is not None]
    if allc and tr_c and mk:
        a_all = sum(allc) / len(allc)
        a_tr = sum(tr_c) / len(tr_c)
        a_mk = sum(mk) / len(mk)
        b_g = sB["gross_avg"]
        L += ["| 环节 | 平均涨幅% | 说明 |", "|---|---:|---|",
              f"| 全部备选（T+1 收盘口径） | {a_all:+.2f} | 含买点未触发票（= 生产展示口径） |",
              f"| 仅可成交票（同上口径） | {a_tr:+.2f} | 剔除 {len(notraded)} 只未触发 |",
              f"| 建仓后 T+1 收盘浮盈 | {a_mk:+.2f} | 从「按 T 收盘计价」改为「按 T+1 开盘建仓」的代价 |",
              f"| T+2 开盘离场（毛） | {b_g:+.2f} | 再叠加 T+1 收盘→T+2 开盘的隔夜漂移 |",
              f"| T+2 开盘离场（净 {FEE_MAIN}%） | {sB['net'][FEE_MAIN]['avg']:+.2f} | 双边费用 |",
              ""]
        L += [f"> 读法：备选池「次日平均涨幅 {a_all:+.2f}%」中，{(a_all - a_tr):+.2f}pp 来自"
              f"买不到（未触发）的票；{(a_tr - a_mk):+.2f}pp 是建仓口径代价；"
              f"{(a_mk - b_g):+.2f}pp 是 T+1 收盘→T+2 开盘的隔夜漂移；{FEE_MAIN}pp 是费用。", ""]
    L += ["", "## 6. 逐票明细", "",
          "| 验证日 | 代码 | 名称 | 组 | 分 | T+1买点 | T+1收盘(不可卖) | 风险位 | "
          "A诊断 | B制度 | C风险 | 备注 |", "|---|---|---|---|---:|---|---:|---|---:|---:|---:|---|"]
    for r in sorted(ev, key=lambda x: x["validation_date"]):
        L.append(f"| {r['validation_date']} | {r['code']} | {r.get('name')} | {r['group']} | "
                 f"{r.get('score')} | {r.get('entry_note') or '—'} | "
                 f"{r.get('mark_t1') if r.get('mark_t1') is not None else '—'} | "
                 f"{'是' if r.get('risk_low_hit') else ('否' if r.get('risk_low_hit') is False else '—')} | "
                 f"{r.get('A') if r.get('A') is not None else '—'} | "
                 f"{r.get('B') if r.get('B') is not None else '—'} | "
                 f"{r.get('C') if r.get('C') is not None else '—'} | {r.get('note') or ''} |")
    L += ["", "> 说明：B/C 为「T+1 买入、T+2 离场」的制度合规口径；日线无法还原盘中路径，"
          "T+2 开盘价与收盘价均视为可成交（跌停情形已单列）。该口径仍是近似："
          "未计滑点差异、未处理停牌顺延。", ""]
    C.write_report("exec_tplus_report.md", "\n".join(L))
    C.write_json("exec_tplus.json", {"records": ev, "A": agg(ev, "A"),
                                     "B": agg(ev, "B"), "C": agg(ev, "C"),
                                     "not_traded": len(notraded), "no_t2": len(noT2)})
    print(f"[c15] ev={len(ev)} traded={len(traded)} not_traded={len(notraded)} no_T2={len(noT2)}")
    for k in ("A", "B", "C"):
        s = agg(ev, k)
        if s.get("n"):
            print(f"[c15] {k}: n={s['n']} gross={s['gross_avg']} "
                  f"net0.15={s['net'][FEE_MAIN]['avg']} win_net={s['net'][FEE_MAIN]['win']}")


if __name__ == "__main__":
    main()
