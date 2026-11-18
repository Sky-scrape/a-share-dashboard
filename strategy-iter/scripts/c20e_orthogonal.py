# -*- coding: utf-8 -*-
"""C20e：低换手是真因子还是「低波动/低成交额」的另一种写法？以及可执行组合。

[1] 正交化：在 OOS 里把 turn 对 (atr_pct, ln amount, cap) 回归取残差，看残差是否还有超额。
[2] 双向排除：高换手 Q5 是 OOS 唯一 t<-3 的负向信号，测试「排除高换手」的可执行价值。
[3] 每日组合：低换手池内每日等权持有，扣双边 0.15%，看净值口径。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def day_t(s, col="r_oc_x"):
    g = s.groupby("date")[col].mean().dropna()
    if len(g) < 4:
        return np.nan, len(g)
    return g.mean() / (g.std(ddof=1) / np.sqrt(len(g))), len(g)


def row(df, mask, label):
    s = df[mask]
    if len(s) < 100:
        return None
    t, nd = day_t(s)
    r = float(s["r_oc"].mean())
    return {"桶": label, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
            "日级t": round(float(t), 2) if t == t else None, "天数": nd,
            "绝对%": round(r, 3), "扣费%": round(r - COST, 3),
            "个股胜率%": round(float((s["r_oc"] > 0).mean() * 100), 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<34s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% t={str(x['日级t']):>6s} "
              f"绝对{x['绝对%']:+7.3f}% 扣费{x['扣费%']:+7.3f}% 股胜{x['个股胜率%']:5.1f}%")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    df["lna"] = np.log(df["amount"].clip(lower=1))
    tr = df[df["date"] <= TRAIN_END]
    turn_q = float(tr["turn"].median())
    turn_hi = float(tr["turn"].quantile(0.8))
    rows = []

    def push(*a):
        for x in a:
            if x:
                rows.append(x)
                show(x)

    print("[1] 正交化：turn 剔除 (atr_pct, ln成交额, ln市值) 后，残差还有没有超额")
    print("    逐日横截面回归，避免跨日水平差异被当成因子")
    df["turn_res"] = np.nan
    for _, g in df.groupby("date"):
        y = g["turn"].values
        X = np.column_stack([np.ones(len(g)), g["atr_pct"].values,
                             g["lna"].values, np.log(g["cap"].clip(lower=1)).values])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 100:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        res = y - X @ b
        df.loc[g.index[ok], "turn_res"] = res[ok]

    for seg, sl in [(df["date"] <= TRAIN_END, "训练"), (df["date"] > TRAIN_END, "OOS")]:
        sub = df[seg & df["turn_res"].notna()]
        q = np.unique(sub["turn_res"].quantile(np.linspace(0, 1, 6)).values)
        lab = pd.cut(sub["turn_res"], bins=q, labels=False, include_lowest=True)
        for b in [0, 4]:
            push(row(df, (lab == b) & seg, f"turn残差 Q{int(b)+1} | {sl}"))
        # 残差最低 20%
        thr = float(sub["turn_res"].quantile(0.2))
        push(row(df, (df["turn_res"] <= thr) & seg, f"turn残差最低20% | {sl}"))

    print("\n[2] 双向排除：高换手 Q5（训练段 80% 分位以上）在 OOS 的负向强度")
    for seg, sl in [(df["date"] <= TRAIN_END, "训练"), (df["date"] > TRAIN_END, "OOS")]:
        push(row(df, seg & (df["turn"] > turn_hi), f"高换手>P80 | {sl}"),
             row(df, seg & (df["turn"] <= turn_hi), f"其余(排除高换手) | {sl}"),
             row(df, seg, f"全市场 | {sl}"))
    print(f"    高换手阈值（训练 P80）= {turn_hi:.2f}%")

    print("\n[3] 可执行：排除高换手后，池内每日等权净值的扣费表现")
    for seg, sl in [(df["date"] <= TRAIN_END, "训练"), (df["date"] > TRAIN_END, "OOS")]:
        s = df[seg & (df["turn"] <= turn_hi)]
        g = s.groupby("date")["r_oc"].mean()
        nav = float((1 + (g - COST) / 100).prod())
        print(f"  {sl}: {len(g)} 日，日均毛 {g.mean():+.3f}%，扣费后日均 "
              f"{(g.mean()-COST):+.3f}%，累计净值 {nav:.4f}，"
              f"日胜率 {((g - COST) > 0).mean()*100:.1f}%")
        rows.append({"桶": f"净值(排高换手) | {sl}", "n": len(s),
                     "超额%": round(float(s["r_oc_x"].mean()), 3),
                     "日级t": round(float(day_t(s)[0]), 2), "天数": len(g),
                     "绝对%": round(g.mean(), 3), "扣费%": round(g.mean() - COST, 3),
                     "个股胜率%": round(float((s["r_oc"] > 0).mean() * 100), 1)})

    print("\n[4] 与全市场等权净值对比（同期间）")
    for seg, sl in [(df["date"] <= TRAIN_END, "训练"), (df["date"] > TRAIN_END, "OOS")]:
        g = df[seg].groupby("date")["r_oc"].mean()
        print(f"  {sl}: 日均 {g.mean():+.3f}%，扣费 {(g.mean()-COST):+.3f}%，"
              f"累计净值 {float((1+(g-COST)/100).prod()):.4f}")

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "c20e_orthogonal.csv",
                                                index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20e_orthogonal.csv'}")


if __name__ == "__main__":
    main()
