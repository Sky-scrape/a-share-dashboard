# -*- coding: utf-8 -*-
"""C20j：市值阈值放到 100 亿，会怎样。

上一轮（C20i）已发现：固定换手≤30%、把市值上限从分位20%一路放宽到不加约束，
超额与 t 值单调上升。所以「放开到 100 亿」方向应该是对的，但要回答四件事：

1. 100 亿在每日横截面里落在哪个分位（会漂移，必须报出来）；
2. 100 亿 vs 30 亿 vs 50 亿 vs 150 亿 vs 不加约束，超额/t/净值 怎么排；
3. 100 亿对应的分位口径结果如何（分位才是可执行写法）；
4. 「30~100 亿」这个中间段单独看，是否比两头都好。

统一口径：换手一律用每日横截面分位（用户要求），市值同时给绝对值与分位两版。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def row(df, mask, label, rows, min_n=80):
    s = df[mask]
    if len(s) < min_n:
        return
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return
    t = gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))
    gm = s.groupby("date")["r_oc"].mean()
    nav = float((1 + (gm - COST) / 100).prod())
    r = {"桶": label, "n": len(s), "日均只数": round(len(s) / len(gm), 0),
         "超额%": round(float(s["r_oc_x"].mean()), 3),
         "日级t": round(float(t), 2), "天数": len(gx),
         "绝对%": round(float(s["r_oc"].mean()), 3),
         "扣费%": round(float(s["r_oc"].mean()) - COST, 3),
         "净值": round(nav, 4),
         "日胜率%": round(float(((gm - COST) > 0).mean() * 100), 1)}
    rows.append(r)
    print(f"  {r['桶']:<34s} n={r['n']:>6d}(日均{r['日均只数']:>4.0f}) "
          f"超额{r['超额%']:+7.3f}% t={r['日级t']:>6.2f} "
          f"绝对{r['绝对%']:+7.3f}% 扣费{r['扣费%']:+7.3f}% "
          f"净值{r['净值']:.4f} 日胜{r['日胜率%']:5.1f}%")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["turn_pct"] = df.groupby("date")["turn"].rank(pct=True)
    df["cap_pct"] = df.groupby("date")["cap"].rank(pct=True)
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    tr, oos = df["date"] <= TRAIN_END, df["date"] > TRAIN_END
    rows = []

    print("[0] 100亿在每日横截面里落在哪个分位")
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        s = df[seg]
        for c in (30, 50, 100, 150):
            pct = float((s["cap"] <= c).groupby(s["date"]).mean().median())
            print(f"  {sl}: cap<={c:>3d}亿 → 覆盖当日中位 {pct*100:4.1f}% 的票 "
                  f"（日均 {(s['cap'] <= c).sum()/s['date'].nunique():.0f} 只）")
        print(f"  {sl}: 当日 cap 中位数 "
              f"{s.groupby('date')['cap'].median().median():.1f}亿")
    print("  → 100 亿约等于「每日市值最低 60~65%」，不是小盘，是大盘以下全体")

    print("\n[1] 换手≤30%分位，市值阈值绝对口径逐档对比")
    for c in (20, 30, 50, 80, 100, 150, 300):
        print(f"  --- 市值 <{c} 亿 + 换手≤30% ---")
        row(df, (df["cap"] < c) & (df["turn_pct"] <= 0.3) & tr, f"cap<{c}亿 训练", rows)
        row(df, (df["cap"] < c) & (df["turn_pct"] <= 0.3) & oos, f"cap<{c}亿 OOS", rows)

    print("\n[2] 对照：不加市值约束")
    row(df, (df["turn_pct"] <= 0.3) & tr, "不加市值 训练", rows)
    row(df, (df["turn_pct"] <= 0.3) & oos, "不加市值 OOS", rows)

    print("\n[3] 换手≤30%，市值改用每日分位（可执行口径）")
    for cp in (0.3, 0.5, 0.6, 0.65, 0.7, 0.8, 1.0):
        row(df, (df["cap_pct"] <= cp) & (df["turn_pct"] <= 0.3) & oos,
            f"cap分位≤{int(cp*100)}% OOS", rows)

    print("\n[4] 「30~100 亿」中间段单独看（是否两头都好、中间最好）")
    for lo, hi in [(0, 30), (30, 50), (50, 100), (100, 150), (150, 1e9)]:
        m = (df["cap"] >= lo) & (df["cap"] < hi) & (df["turn_pct"] <= 0.3)
        nm = f"{lo:.0f}-{hi:.0f}亿" if hi < 1e9 else f">{lo:.0f}亿"
        row(df, m & tr, f"{nm} 低换手 训练", rows, min_n=50)
        row(df, m & oos, f"{nm} 低换手 OOS", rows, min_n=50)

    print("\n[5] cap<100亿 + 换手≤30% 的逐月路径")
    m = (df["cap"] < 100) & (df["turn_pct"] <= 0.3)
    for mm in ["2026-07", "2026-08", "2026-09", "2026-10"]:
        row(df, m & (df["mon"] == mm), f"cap<100亿 低换手 {mm[5:]}月", rows)

    print("\n[6] 净值对照（同期间，扣双边 0.15%）")
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        gm = df[seg].groupby("date")["r_oc"].mean()
        print(f"  {sl} 全市场基线: {len(gm)}日 日均毛{gm.mean():+.3f}% "
              f"扣费{(gm.mean()-COST):+.3f}% 净值{float((1+(gm-COST)/100).prod()):.4f}")

    print("\n[7] 换手分位与市值分位的交互（cap<100亿 内，换手档位扫描）")
    for tp in (0.1, 0.2, 0.3, 0.5):
        row(df, (df["cap"] < 100) & (df["turn_pct"] <= tp) & oos,
            f"cap<100亿 + 换手≤{int(tp*100)}% OOS", rows, min_n=50)

    pd.DataFrame(rows).to_csv(OUT / "c20j_cap100.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20j_cap100.csv'} ({len(rows)} 桶)")


if __name__ == "__main__":
    main()
