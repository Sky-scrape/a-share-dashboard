# -*- coding: utf-8 -*-
"""C7 逐因素消融审计：单一局部关闭 × 同窗口同口径，量化每个因素的边际贡献。

背景（2026-09-10，用户要求）：C7 纳入大量因素，逐一检验「去掉它整体会不会变差/
变好」，为「通过局部修改实现整体效益最大化」提供证据。本脚本**只做审计不改策略**：
全部变体在内存中构造（deepcopy C7 后改一个键），策略文件零改动。

变体清单（24 项，全部与 C7 基线共用同一窗口 2026-01-05..2026-09-08、同一
执行口径 backtest_capital.trade_pnl realtime、同一预计算——除「删隔夜闸门」
改变了 regime 需要 eigene precompute）：
  A 涨停组打分因子 weight→0：theme/concept/ladder/seal/time/liq/struct
  B 低吸组打分因子 weight→0：sector/trend/pos/mom/liq/recog
  C 涨停组结构开关：排一字/排尾盘板/独狼板/概念独狼/成交额门槛 逐个关闭
  D 低吸组结构开关：排昨日涨停/行业分位下限/量能闸门/成交额门槛 逐个关闭
  E 闸门与门槛：删隔夜美股闸门；入选门槛 70→55

判定（跑数前预注册，对 realtime 均益差 Δ=变体−基线）：
  Δ ≤ −0.30pp → 该因素【有正贡献】（去掉变差，保留）
  |Δ| < 0.30pp → 【零边际】（在 C7 结构与配额下无可测贡献；注意配额可能掩盖
                 排序深度价值，附 factors 分桶梯度作佐证）
  Δ ≥ +0.30pp → 【负贡献】（去掉变好，下轮迭代候选）
  CI95 为两样本 bootstrap（2000 次，seed=7）；n<30 的变体只列数不下结论。

用法：
    python strategy-iter/scripts/c8_factor_ablation.py --run runs/c8_window_C7
输出：<run>/factor_ablation_report.md + 控制台摘要表。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

import backtest_capital as bt  # noqa: E402  执行口径单一来源
from engine.data import build_store, SEL_START, SEL_END  # noqa: E402
from engine.precompute import precompute  # noqa: E402
from engine.select import select_limit_up, select_non_lu, in_pick_universe  # noqa: E402
from engine.validate import validate_pick  # noqa: E402
from engine import rules as R  # noqa: E402

BOOT_N, BOOT_SEED = 2000, 7
TH = 0.30  # 判定阈值 pp


# ---------------------------------------------------------------- 变体定义

def _variants() -> list[tuple[str, object]]:
    """(变体名, mutator)；mutator 就地修改 deepcopy 后的 cfg。返回顺序即报告顺序。"""
    def w0(group, key):
        def m(cfg):
            cfg[group]["weights"][key] = 0
        return m
    vs = []
    for k in ("theme", "concept", "ladder", "seal", "time", "liq", "struct"):
        vs.append((f"LU因子 {k} 权重→0", w0("lu_group", k)))
    for k in ("sector", "trend", "pos", "mom", "liq", "recog"):
        vs.append((f"NLU因子 {k} 权重→0", w0("nlu_group", k)))

    def flag(group, key, val):
        def m(cfg):
            cfg[group][key] = val
        return m
    vs += [
        ("LU开关 排一字关闭", flag("lu_group", "exclude_prev_yizi", False)),
        ("LU开关 不排尾盘板", flag("lu_group", "exclude_late_time", "23:59")),
        ("LU开关 独狼板不排除", flag("lu_group", "theme_min_members", 0)),
        ("LU开关 概念独狼不排除", flag("lu_group", "exclude_concept_lonewolf", False)),
        ("LU开关 成交额门槛取消", flag("lu_group", "min_amount", 0)),
        ("NLU开关 排昨日涨停关闭", flag("nlu_group", "exclude_prev_lu", False)),
        ("NLU开关 行业分位下限取消", flag("nlu_group", "sector_rank_min", None)),
        ("NLU开关 量能闸门取消", flag("nlu_group", "market_amt_ratio_min", None)),
        ("NLU开关 成交额门槛取消", flag("nlu_group", "min_amount", 0)),
    ]

    def no_us_gate(cfg):
        cfg["regime"] = {k: v for k, v in cfg["regime"].items()
                         if k != "us_night_force_defensive"}
    vs.append(("闸门 删隔夜美股闸门", no_us_gate))

    def ms55(cfg):
        cfg["lu_group"]["min_score"] = 55.0
        cfg["nlu_group"]["min_score"] = 55.0
    vs.append(("门槛 70→55（回C6）", ms55))
    return vs


# ---------------------------------------------------------------- 选股+验证循环

def run_cfg(cfg, store, pre) -> list[dict]:
    """复刻 run_round 主循环（不落盘、不做范围断言），返回 validation 行列表。"""
    ms = pre["ms"].set_index("date")
    si = pre["si"]
    si_by_date = {d: g for d, g in si.groupby("date")}
    tdays = [d for d in store.trade_dates if SEL_START <= d <= SEL_END]
    out = []
    for T in tdays:
        T1 = store.next_trade_date(T)
        if T1 is None:
            continue
        mrow = ms.loc[T] if T in ms.index else None
        regime = mrow["regime"] if mrow is not None else "normal"
        qlu, qnlu = cfg["quotas"][regime]
        lu_picks = select_limit_up(pre, store, T, qlu, cfg["lu_group"])
        si_day = si_by_date.get(T)
        nlu_picks = select_non_lu(pre, store, si_day if si_day is not None else si.iloc[0:0],
                                  mrow, T, qnlu, cfg["nlu_group"])
        for p, gcfg in [(p, cfg["lu_group"]) for p in lu_picks] + \
                       [(p, cfg["nlu_group"]) for p in nlu_picks]:
            assert in_pick_universe(p["thscode"], gcfg), f"选股范围违规: {p['thscode']}"
        for p in lu_picks + nlu_picks:
            v = validate_pick(p, store, pre, T, T1, cfg)
            if v is None:
                continue
            v["regime"] = regime
            out.append(v)
    return out


def to_bt_rows(val_rows: list[dict]) -> list[dict]:
    rows = []
    for v in val_rows:
        rows.append({"T": v["T"], "T1": v["T1"], "group": v["group"],
                     "thscode": v["thscode"], "name": v.get("name") or v["thscode"],
                     "regime": v.get("regime") or "",
                     "o": v.get("open_ret"), "h": v.get("high_ret"),
                     "l": v.get("low_ret"), "c": v.get("close_ret"),
                     "buy_triggered": bool(v.get("buy_triggered"))})
    return rows


# ---------------------------------------------------------------- 评估

def evaluate(val_rows: list[dict]) -> dict:
    rows = to_bt_rows(val_rows)
    pnl = [p * 100.0 for p in (bt.trade_pnl(r) for r in rows) if p is not None]
    cr = [r["c"] for r in rows if r["c"] is not None]
    _, eq, mdd = bt.build_curve(rows, mode="realtime")
    g = {}
    for grp in ("lu", "nlu"):
        rs = [r for r in rows if r["group"] == grp]
        gp = [p * 100.0 for p in (bt.trade_pnl(r) for r in rs) if p is not None]
        gc = [r["c"] for r in rs if r["c"] is not None]
        g[grp] = {"n": len(rs), "exec": len(gp),
                  "pnl_avg": round(float(np.mean(gp)), 3) if gp else None,
                  "close_avg": round(float(np.mean(gc)), 3) if gc else None,
                  "win": round(float(np.mean(np.array(gc) > 0) * 100), 1) if gc else None}
    return {"n": len(rows), "exec": len(pnl),
            "pnl_avg": round(float(np.mean(pnl)), 3) if pnl else None,
            "close_avg": round(float(np.mean(cr)), 3) if cr else None,
            "win": round(float(np.mean(np.array(cr) > 0) * 100), 1) if cr else None,
            "equity": round(eq, 4), "maxdd": round(mdd * 100, 1), "groups": g,
            "_pnl": pnl, "_rows": rows}


def boot_ci(a: list[float], b: list[float]):
    """均值差的两样本 bootstrap 95% CI（pp）。

    入参是 evaluate() 的 `_pnl`（已是 pp，勿再乘 100——2026-09-10 修正此前的
    二次缩放 bug，它把 CI 放大了 100 倍）。
    """
    if len(a) < 30 or len(b) < 30:
        return None
    rng = np.random.default_rng(BOOT_SEED)
    aa, bb = np.asarray(a), np.asarray(b)
    d = aa[rng.integers(0, len(aa), (BOOT_N, len(aa)))].mean(axis=1) \
        - bb[rng.integers(0, len(bb), (BOOT_N, len(bb)))].mean(axis=1)
    return round(float(np.percentile(d, 2.5)), 2), round(float(np.percentile(d, 97.5)), 2)


def verdict(delta_pp: float | None, n: int) -> str:
    if delta_pp is None or n < 30:
        return "样本不足"
    if delta_pp <= -TH:
        return "有正贡献→保留"
    if delta_pp >= TH:
        return "负贡献→下轮候选"
    return "零边际"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="C7 逐因素消融审计（只读，不改策略）")
    ap.add_argument("--run", default="runs/c8_window_C7",
                    help="基线 run 目录（校验一致性 + 报告输出位置）")
    args = ap.parse_args()
    run_dir = BASE / args.run

    print("[1/4] build_store ...", flush=True)
    store = build_store()
    cfg7 = R.VERSIONS["C7"]
    print("[2/4] precompute (C7 regime) ...", flush=True)
    pre7 = precompute(store, cfg7)

    print("[3/4] 基线 + 24 变体选股验证 ...", flush=True)
    base = evaluate(run_cfg(cfg7, store, pre7))
    # 一致性校验：与已落盘 run 同口径
    ref = pd.read_csv(run_dir / "validation.csv")
    if len(ref) != base["n"]:
        print(f"[warn] 基线 n={base['n']} 与落盘 run n={len(ref)} 不一致，以本次为准")

    results = []
    own_pre = None
    for name, mut in _variants():
        cfg = copy.deepcopy(cfg7)
        mut(cfg)
        pre = pre7
        if "us_night_force_defensive" not in cfg["regime"]:
            if own_pre is None:
                print("    （删隔夜闸门需要自己的 regime 预计算）", flush=True)
                own_pre = precompute(store, cfg)
            pre = own_pre
        r = evaluate(run_cfg(cfg, store, pre))
        d = (round(r["pnl_avg"] - base["pnl_avg"], 3)
             if r["pnl_avg"] is not None and base["pnl_avg"] is not None else None)
        results.append({"name": name, "m": r, "d_pp": d,
                        "ci": boot_ci(r["_pnl"], base["_pnl"]),
                        "dn": r["n"] - base["n"]})
        if d is not None:
            print(f"  {name}: n={r['n']} ({r['n'] - base['n']:+d}) "
                  f"实时均益={r['pnl_avg']}% (Δ{d:+.2f}pp)", flush=True)
        else:
            print(f"  {name}: n={r['n']}", flush=True)

    print("[4/4] 写报告 ...", flush=True)
    L = ["# C7 逐因素消融审计报告", "",
         f"- 基线 C7：n={base['n']} · 条件均次 {base['close_avg']}% · 胜率 {base['win']}% · "
         f"实时均益 {base['pnl_avg']}% · equity {base['equity']} · maxDD {base['maxdd']}%",
         "- 判定预注册：Δ=变体−基线（实时均益 pp）；Δ≤−0.30 有正贡献；|Δ|<0.30 零边际；"
         "Δ≥+0.30 负贡献；n<30 只列数。CI95=bootstrap2000 次。",
         "- 读法：CI95 量纲为 pp（2026-09-10 修正此前二次 ×100 的放大 bug）；本窗口多数 Δ 的 CI "
         "跨零（n≈430 对 0.3~0.5pp 差异功效不足），判定以点估计为主，动规则须连续两轮同向复核。", ""]
    sec_map = [("A 涨停组打分因子", "LU因子"), ("B 低吸组打分因子", "NLU因子"),
               ("C 涨停组结构开关", "LU开关"), ("D 低吸组结构开关", "NLU开关"),
               ("E 闸门与门槛", None)]
    for title, prefix in sec_map:
        L += [f"## {title}", "",
              "| 变体 | n(Δ) | 条件均次% | 胜率% | 实时均益% | Δ pp | CI95 | equity | 判定 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for x in results:
            if prefix and not x["name"].startswith(prefix):
                continue
            if prefix is None and x["name"].startswith(("LU", "NLU")):
                continue
            m, d = x["m"], x["d_pp"]
            L.append(f"| {x['name']} | {m['n']} ({x['dn']:+d}) | {m['close_avg']} | {m['win']} "
                     f"| {m['pnl_avg']} | {d:+.2f} | {x['ci']} | {m['equity']} "
                     f"| {verdict(d, m['exec'])} |")
        L.append("")
    L += ["## 分组明细（涨停组/低吸组实时均益）", "",
          "| 变体 | LU n/exec/均益 | NLU n/exec/均益 |", "|---|---|---|"]
    for x in results:
        g = x["m"]["groups"]
        fmt = lambda k: (f"{g[k]['n']}/{g[k]['exec']}/{g[k]['pnl_avg']}" if g[k]["pnl_avg"] is not None
                         else f"{g[k]['n']}/{g[k]['exec']}/-")
        L.append(f"| {x['name']} | {fmt('lu')} | {fmt('nlu')} |")
    L += ["", "注：零边际≠因子无价值——配额制下它仍参与排序竞争；附 picks.csv factors 列可做"
              "分桶梯度佐证。负贡献项进入下轮迭代候选清单，须经完整轮次评审再动规则。"]

    out = run_dir / "factor_ablation_report.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"报告: {out}")
    print(json.dumps([{"v": x["name"], "n": x["m"]["n"], "pnl": x["m"]["pnl_avg"],
                       "d": x["d_pp"], "verdict": verdict(x["d_pp"], x["m"]["exec"])}
                      for x in results], ensure_ascii=False, indent=0))


if __name__ == "__main__":
    main()
