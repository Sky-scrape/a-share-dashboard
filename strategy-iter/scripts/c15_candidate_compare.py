# -*- coding: utf-8 -*-
"""C15 复审 · 候选对照：C7 vs 5 个候选，在「7 月至今」窗口上按制度口径比较（只读）。

输入：`runs/c15_cand_*/validation.csv`（engine.run_round 产物，窗口 2026-01-02..10-14）。
比较窗口：
  - 开发窗 2026-01-02 ~ 2026-06-30（预注册的开发区间）
  - **验证窗 2026-07-01 ~ 2026-10-14（用户要求的「7 月至今」）**
指标（全部用绝对前复权价现算，不读 CSV 的比率字段——run_round 的布尔率字段在本轮
输出中异常，已核实 validation.csv 本身正确）：
  - 收红率 / 平均次日涨幅（条件期望口径，与生产展示一致）
  - 可成交笔数（实时买点口径）、制度口径净收益（B: T+2 开盘离场 / C: T+2 风险感知）
  - 日级配对 bootstrap：逐验证日取当日组合净均，比较候选与基线的日均差 95% CI

判定阈值见 runs/c15_oos_review/preregistration.md（Δ≥+0.30pp、CI 下界>0、半窗同向、
成交笔数≥基线 70%、任一组 Δ≤−0.30pp 即淘汰）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend" / "recap"))

import numpy as np                                  # noqa: E402
import pandas as pd                                 # noqa: E402

import c15_common as C                              # noqa: E402
from c15_exec_tplus import MF_BUY, entry_of, _pct, _ratio, FEE_MAIN  # noqa: E402

RUNS = C.BASE / "strategy-iter" / "runs"
VERSIONS = ["C7", "C15A", "C15B", "C15C", "C15D", "C15E", "C15F", "C15G"]
DEV = ("2026-01-02", "2026-06-30")
VAL = ("2026-07-01", "2026-10-14")


def load_val(version):
    p = RUNS / f"c15_cand_{version}" / "validation.csv"
    if not p.exists():
        return None
    v = pd.read_csv(p, dtype=str)
    v["T"] = v["T"].astype(str)
    for c in ("open_ret", "high_ret", "low_ret", "close_ret", "pick_score"):
        v[c] = pd.to_numeric(v[c], errors="coerce")
    for c in ("in_window", "buy_triggered", "risk_hit", "win"):
        if c in v:
            v[c] = v[c].map({"True": True, "False": False})
    return v


def evaluate(version, bars, cal):
    """逐票算制度口径收益；返回 DataFrame（含 T / group / close / B / C / triggered）。"""
    v = load_val(version)
    if v is None or v.empty:
        return None
    codes = sorted({str(t).split(".")[0] for t in v["thscode"]})
    have = C.daily_bars(codes, "2026-01-01", cal[-1])
    rows = []
    for _, r in v.iterrows():
        code = str(r["thscode"]).split(".")[0]
        grp = r["group"]
        T, T1 = r["T"], str(r["T1"])
        t2 = C.next_trade_day(cal, T1)
        b = have.get(code) or {}
        bar0, bar1 = b.get(T), b.get(T1)
        bar2 = b.get(t2) if t2 else None
        out = {"T": T, "T1": T1, "group": grp, "code": code, "close": r["close_ret"],
               "win": r["win"], "triggered": False, "B": None, "C": None}
        if not bar0 or bar1 is None or grp not in MF_BUY:
            rows.append(out)
            continue
        c0 = bar0["c"]
        e_ret, _why = entry_of(grp, bar1["o"], bar1["h"], c0)
        if e_ret is None:
            rows.append(out)
            continue
        out["triggered"] = True
        l1 = _pct(bar1["l"], c0)
        c1 = _pct(bar1["c"], c0)
        if bar2 is None:
            rows.append(out)
            continue
        r2o, r2c = _pct(bar2["o"], c0), _pct(bar2["c"], c0)
        out["B"] = round(_ratio(r2o, e_ret) - FEE_MAIN, 3)
        out["C"] = round(_ratio(r2o if l1 <= MF_BUY[grp]["risk_low"] else r2c, e_ret) - FEE_MAIN, 3)
        rows.append(out)
    return pd.DataFrame(rows)


def window(df, lo, hi):
    return df[(df["T"] >= lo) & (df["T"] <= hi)]


def metrics(df, key="C"):
    if df is None or df.empty:
        return None
    n = len(df)
    ex = df[df[key].notna()]
    return {
        "n": n,
        "win_rate": round(100.0 * df["win"].mean(), 1),
        "avg_close": round(df["close"].mean(), 3),
        "n_exec": len(ex),
        "exec_net": round(ex[key].mean(), 3) if len(ex) else None,
        "exec_win": round(100.0 * (ex[key] > 0).mean(), 1) if len(ex) else None,
    }


def daily_series(df, key="C"):
    """逐验证日组合净均（无成交=0，空仓口径）。"""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    g = df.groupby("T1")[key].apply(lambda s: s.dropna().mean() if s.notna().any() else 0.0)
    return g


def paired_ci(base, cand, n_boot=10000, seed=7):
    """日级配对 bootstrap：日均差 (cand-base) 的 95% CI。"""
    idx = base.index.union(cand.index)
    b = base.reindex(idx).fillna(0.0)
    a = cand.reindex(idx).fillna(0.0)
    d = (a - b).to_numpy()
    if not len(d):
        return None
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, len(d), size=(n_boot, len(d)))].mean(axis=1)
    return {
        "n_days": int(len(d)),
        "delta": round(float(d.mean()), 3),
        "lo": round(float(np.percentile(means, 2.5)), 3),
        "hi": round(float(np.percentile(means, 97.5)), 3),
        "P(>0)": round(float((means > 0).mean()), 3),
    }


def main():
    cal = C.trade_calendar()
    ev = {}
    for v in VERSIONS:
        d = evaluate(v, None, cal)
        if d is None:
            print(f"[cand] {v}: 缺 validation.csv")
            continue
        ev[v] = d
    if "C7" not in ev:
        print("基线缺失，终止")
        return

    L = ["# C15 · 候选对照（7 月至今 + 开发窗）", "",
         "口径：制度口径 T+1 买 / T+2 离场、双边费用 0.15%、日级等权空仓计价；"
         "「收红率/平均次日」为条件期望口径（含买点未触发票）。"
         "判定阈值见 [preregistration.md](preregistration.md)。", ""]
    for label, (lo, hi) in (("验证窗 2026-07-01 ~ 2026-10-14（用户指定）", VAL),
                            ("开发窗 2026-01-02 ~ 2026-06-30", DEV)):
        L += [f"## {label}", "",
              "| 版本 | 选票数 | 收红率% | 平均次日% | 可成交 | 制度净均%(C) | 制度净收红率% | "
              "Δ净均 vs C7 | 日级配对 Δ 95%CI | 成交数比 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
        base = window(ev["C7"], lo, hi)
        m0 = metrics(base)
        sb = daily_series(base)
        for v in VERSIONS:
            if v not in ev:
                continue
            m = metrics(window(ev[v], lo, hi))
            if not m or not m["n"]:
                continue
            ci = paired_ci(sb, daily_series(window(ev[v], lo, hi)))
            delta = (round(m["exec_net"] - m0["exec_net"], 3)
                     if (m["exec_net"] is not None and m0["exec_net"] is not None) else None)
            ratio = round(100.0 * m["n_exec"] / m0["n_exec"], 1) if m0["n_exec"] else None
            ci_s = f"{ci['delta']:+.3f} [{ci['lo']:+.3f}, {ci['hi']:+.3f}]" if ci else "—"
            star = " **基线**" if v == "C7" else ""
            L.append(f"| {v}{star} | {m['n']} | {m['win_rate']} | {m['avg_close']} | "
                     f"{m['n_exec']} | {m['exec_net']} | {m['exec_win']} | "
                     f"{delta if delta is not None else '—'} | {ci_s} | {ratio}% |")
        L.append("")
    L += ["## 判定（按预注册阈值机械核对）", ""]
    base_v = window(ev["C7"], *VAL)
    m0 = metrics(base_v)
    sb = daily_series(base_v)
    for v in VERSIONS:
        if v == "C7" or v not in ev:
            continue
        m = metrics(window(ev[v], *VAL))
        if not m or not m["n"]:
            continue
        ci = paired_ci(sb, daily_series(window(ev[v], *VAL)))
        checks = []
        d = (m["exec_net"] - m0["exec_net"]) if (m["exec_net"] is not None
                                                and m0["exec_net"] is not None) else None
        checks.append(("Δ≥+0.30pp", d is not None and d >= 0.30))
        checks.append(("CI 下界>0", ci is not None and ci["lo"] > 0))
        checks.append(("成交数≥基线70%", m0["n_exec"] and m["n_exec"] >= 0.7 * m0["n_exec"]))
        dev_d = None
        md = metrics(window(ev[v], *DEV))
        m0d = metrics(window(ev["C7"], *DEV))
        if md and m0d and md["exec_net"] is not None and m0d["exec_net"] is not None:
            dev_d = md["exec_net"] - m0d["exec_net"]
            checks.append(("半窗同向", (d is not None and dev_d is not None
                                     and (d > 0) == (dev_d > 0))))
        npass = sum(1 for _k, ok in checks if ok)
        verdict = "通过" if all(ok for _k, ok in checks) else "未通过"
        L.append(f"- **{v}**：{verdict}（{npass}/{len(checks)} 项）—— "
                 + "；".join(f"{k}{'✓' if ok else '✗'}" for k, ok in checks)
                 + (f"；开发窗 Δ={dev_d:+.3f}" if dev_d is not None else ""))
    L += ["", "> 说明：日级配对 bootstrap 以「验证日」为配对单位（候选与基线选票不同，"
          "逐票配对不成立），故比较的是**每日组合净均**的分布差；"
          "未计成交冲击与涨跌停排队，详见 exec 报告边界披露。", ""]
    C.write_report("candidate_compare.md", "\n".join(L))
    print("\n".join(L[-12:]))


if __name__ == "__main__":
    main()
