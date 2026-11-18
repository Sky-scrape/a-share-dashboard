# -*- coding: utf-8 -*-
"""C20g：把「小市值」从绝对阈值换成每日横截面分位，再验一次「低市值+低换手」。

为什么要换口径
--------------
C20d 用的是绝对阈值 cap < 30亿。这个阈值有两个毛病：
1. 会漂移：7-8 月落在 30 亿以下的票有 29,331 行，9-10 月只有 23,399 行，
   池子本身在缩，等于样本外悄悄换了池；
2. 不可执行：实盘你没法预知「今年 30 亿算小」，只能每天看相对排名。

所以这里改成**每日横截面分位**：每天按当日流通市值排序取最低 p%，
当日收盘就能算出来，T 日收盘买入时点合法。换手率同样用每日分位。

检验两种阈值来源：
- 固定分位（p 值写死）：最朴素，也最不容易过拟合；
- 这之外的交叉组合，看有没有哪个角落能活过样本外。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def add_pct_rank(df):
    """每日横截面分位（0~1，越小越低）。当日收盘即可算出，无未来信息。"""
    g = df.groupby("date")
    df["cap_pct"] = g["cap"].rank(pct=True)
    df["turn_pct"] = g["turn"].rank(pct=True)
    df["amount_pct"] = g["amount"].rank(pct=True)
    df["atr_pct_rank"] = g["atr_pct"].rank(pct=True)
    return df


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
    gm = s.groupby("date")["r_oc"].mean()
    return {"桶": label, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
            "日级t": round(float(t), 2) if t == t else None, "天数": nd,
            "绝对%": round(float(s["r_oc"].mean()), 3),
            "扣费%": round(float(s["r_oc"].mean()) - COST, 3),
            "净值": round(float((1 + (gm - COST) / 100).prod()), 4),
            "日胜率%": round(float(((gm - COST) > 0).mean() * 100), 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<34s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% "
              f"t={str(x['日级t']):>6s} 绝对{x['绝对%']:+7.3f}% "
              f"扣费{x['扣费%']:+7.3f}% 净值{x['净值']:.4f} 日胜{x['日胜率%']:5.1f}%")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    df = add_pct_rank(df)
    tr = df["date"] <= TRAIN_END
    oos = df["date"] > TRAIN_END
    rows = []

    def push(*a):
        for x in a:
            if x:
                rows.append(x)
                show(x)

    print("[0] 池子稳定性对比：绝对阈值 vs 每日分位")
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        a = df[seg & (df["cap"] < 30)]
        b = df[seg & (df["cap_pct"] <= 0.3)]
        print(f"  {sl}: cap<30亿 {len(a):>6d} 行/日均 {len(a)/df[seg]['date'].nunique():5.0f} | "
              f"cap分位≤30% {len(b):>6d} 行/日均 {len(b)/df[seg]['date'].nunique():5.0f}")

    print("\n[1] 核心：低市值分位 × 低换手分位（四种组合，三档严格度）")
    for cp in (0.2, 0.3, 0.5):
        for tp in (0.2, 0.3, 0.5):
            m = (df["cap_pct"] <= cp) & (df["turn_pct"] <= tp)
            nm = f"市值≤{int(cp*100)}%+换手≤{int(tp*100)}%"
            push(row(df, m & tr, f"{nm} | 训练"), row(df, m & oos, f"{nm} | OOS"))
        print()

    print("[2] 只看最低市值档 + 最低换手档（最严组合）的逐月")
    m = (df["cap_pct"] <= 0.2) & (df["turn_pct"] <= 0.2)
    for mm in ["2026-07", "2026-08", "2026-09", "2026-10"]:
        push(row(df, m & (df["mon"] == mm), f"市值20%+换手20% | {mm[5:]}月"))

    print("\n[3] 对照：单用市值分位 / 单用换手分位")
    for cp in (0.2, 0.3):
        push(row(df, (df["cap_pct"] <= cp) & tr, f"市值≤{int(cp*100)}% | 训练"),
             row(df, (df["cap_pct"] <= cp) & oos, f"市值≤{int(cp*100)}% | OOS"))
    for tp in (0.2, 0.3):
        push(row(df, (df["turn_pct"] <= tp) & tr, f"换手≤{int(tp*100)}% | 训练"),
             row(df, (df["turn_pct"] <= tp) & oos, f"换手≤{int(tp*100)}% | OOS"))

    print("\n[4] 分位版本的正交化：剔除波动/成交额后残差还有多少")
    df["turn_res"] = np.nan
    for _, g in df.groupby("date"):
        y = g["turn_pct"].values
        X = np.column_stack([np.ones(len(g)), g["atr_pct_rank"].values,
                             g["amount_pct"].values, g["cap_pct"].values])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 100:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        df.loc[g.index[ok], "turn_res"] = (y - X @ b)[ok]
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        sub = df[seg & df["turn_res"].notna()]
        thr = float(sub["turn_res"].quantile(0.2))
        push(row(df, (df["turn_res"] <= thr) & seg, f"换手残差最低20% | {sl}"))

    print("\n[5] 逐日等权净值：最严组合 vs 全市场（扣双边 0.15%）")
    m_best = (df["cap_pct"] <= 0.2) & (df["turn_pct"] <= 0.2)
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        for nm, m in [("全市场", pd.Series(True, index=df.index)),
                      ("市值20%+换手20%", m_best)]:
            g = df[seg & m].groupby("date")["r_oc"].mean()
            nav = float((1 + (g - COST) / 100).prod())
            print(f"  {sl} {nm:<16s} {len(g):>2d}日 日均毛{g.mean():+7.3f}% "
                  f"扣费日{(g.mean()-COST):+7.3f}% 净值{nav:.4f} "
                  f"日胜率{((g-COST)>0).mean()*100:5.1f}%")

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "c20g_pctrank.csv",
                                                index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20g_pctrank.csv'} ({len(rows)} 桶)")


if __name__ == "__main__":
    main()
