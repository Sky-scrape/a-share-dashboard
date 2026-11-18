# -*- coding: utf-8 -*-
"""C20f：把背离结论按月份拆开，检查它是不是只在某一个月成立（防过拟合）。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
from c20c_divergence import melt_flow, prep, TRAIN_END  # noqa: E402

OUT = BASE / "runs" / "c20_moneyflow"


def ex(df, m, lab, rows):
    s = df[m]
    if len(s) < 50:
        return
    g = s.groupby("date")["r_oc_x"].mean()
    if len(g) < 3:
        return
    t = g.mean() / (g.std(ddof=1) / np.sqrt(len(g)))
    r = {"桶": lab, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
         "日级t": round(float(t), 2), "天数": len(g),
         "绝对%": round(float(s["r_oc"].mean()), 3)}
    rows.append(r)
    print(f"  {lab:<30s} n={r['n']:>6d} 超额{r['超额%']:+7.3f}% "
          f"t={r['日级t']:>6.2f} ({r['天数']}日) 绝对{r['绝对%']:+7.3f}%")


def main():
    f2 = [f for f in sorted((BASE / "raw" / "moneyflow2").glob("flow2_*.json"))
          if ".part" not in f.name]
    fl = melt_flow(f2, {"特大单净买入额", "小单净买入额"}, "flow2")
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].merge(
        fl.rename(columns={"特大单净买入额": "xl", "小单净买入额": "sm"}),
        on=["thscode", "date"])
    df = prep(df)
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    tq = float(df[df["date"] <= TRAIN_END]["turn"].median())
    pool = (df["cap"] < 30) & (df["turn"] <= tq)
    rows = []

    print("[1] 全市场「主力买+散户卖」逐月（看是否单月驱动）")
    for m in ["2026-07", "2026-08", "2026-09"]:
        ex(df, (df["mon"] == m) & (df["xl"] > 0) & (df["sm"] < 0), f"{m[-2:]}月 背离", rows)
    print()
    print("[2] 有效池「小市值+低换手」内，逐月 × 背离")
    for m in ["2026-07", "2026-08", "2026-09"]:
        print(f"  {m}")
        ex(df, pool & (df["mon"] == m), "  池内全", rows)
        ex(df, pool & (df["mon"] == m) & (df["xl"] > 0) & (df["sm"] < 0),
           "  池内+主力买散户卖", rows)
        ex(df, pool & (df["mon"] == m) & (df["xl"] < 0) & (df["sm"] > 0),
           "  池内+主力卖散户买", rows)

    print("\n[3] 背离的机制核对：背离组当日的平均涨幅（是否只是「已涨过」）")
    for nm, m in [("主力买散户卖", (df["xl"] > 0) & (df["sm"] < 0)),
                  ("主力卖散户买", (df["xl"] < 0) & (df["sm"] > 0)),
                  ("主力买散户买", (df["xl"] > 0) & (df["sm"] > 0)),
                  ("主力卖散户卖", (df["xl"] < 0) & (df["sm"] < 0))]:
        s = df[m]
        print(f"  {nm:<8s} n={len(s):>6d} 当日涨幅均值 {s['chg'].mean():+6.2f}% "
              f"中位 {s['chg'].median():+6.2f}% 20日涨幅均值 {s['ret20'].mean():+6.2f}%")

    print("\n[4] 控制当日涨幅后，背离是否还有增量（分档内比较）")
    df["chg_q"] = pd.cut(df["chg"], bins=[-20, -3, 0, 3, 6, 9.5, 30], labels=False)
    for b in sorted(pd.unique(df["chg_q"].dropna())):
        s = df[df["chg_q"] == b]
        a = s[(s["xl"] > 0) & (s["sm"] < 0)]
        c = s[(s["xl"] < 0) & (s["sm"] > 0)]
        if len(a) < 100 or len(c) < 100:
            continue
        print(f"  涨幅档 Q{int(b)+1}: 背离组 {a['r_oc_x'].mean():+.3f}% (n={len(a)}) | "
              f"反背离 {c['r_oc_x'].mean():+.3f}% (n={len(c)}) | "
              f"差 {a['r_oc_x'].mean()-c['r_oc_x'].mean():+.3f}pp")

    pd.DataFrame(rows).to_csv(OUT / "c20f_monthly.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20f_monthly.csv'}")


if __name__ == "__main__":
    main()
