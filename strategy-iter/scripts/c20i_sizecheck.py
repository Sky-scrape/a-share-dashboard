# -*- coding: utf-8 -*-
"""C20i：低市值到底加不加分？——把「低市值」这一项单独拎出来做增减检验。

C20h 暴露了一个反直觉现象：
- 低换手在**五个市值分层里都有效**，且市值越高越强（最高20%档 OOS +0.255%）；
- 纯「换手≤30%」的 OOS 净值 0.8460，反而高于「低市值+低换手」的 0.8274。

也就是说「低市值」这一条可能在做负贡献。这里把它彻底验一遍：
在其他条件相同的池子里，逐步加入/移除市值约束，看超额与净值怎么动。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def row(df, mask, label, rows):
    s = df[mask]
    if len(s) < 80:
        return
    g = s.groupby("date")["r_oc_x"].mean()
    t = g.mean() / (g.std(ddof=1) / np.sqrt(len(g))) if len(g) > 3 else np.nan
    gm = s.groupby("date")["r_oc"].mean()
    nav = float((1 + (gm - COST) / 100).prod())
    r = {"桶": label, "n": len(s), "日均只数": round(len(s) / len(gm), 0),
         "超额%": round(float(s["r_oc_x"].mean()), 3),
         "日级t": round(float(t), 2) if t == t else None, "天数": len(g),
         "绝对%": round(float(s["r_oc"].mean()), 3),
         "扣费%": round(float(s["r_oc"].mean()) - COST, 3),
         "净值": round(nav, 4),
         "日胜率%": round(float(((gm - COST) > 0).mean() * 100), 1)}
    rows.append(r)
    print(f"  {r['桶']:<30s} n={r['n']:>6d}(日均{r['日均只数']:>4.0f}) "
          f"超额{r['超额%']:+7.3f}% t={str(r['日级t']):>6s} "
          f"扣费{r['扣费%']:+7.3f}% 净值{r['净值']:.4f} 日胜{r['日胜率%']:5.1f}%")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    g = df.groupby("date")
    df["cap_pct"] = g["cap"].rank(pct=True)
    df["turn_pct"] = g["turn"].rank(pct=True)
    tr, oos = df["date"] <= TRAIN_END, df["date"] > TRAIN_END
    rows = []

    print("[1] 固定换手≤30%，逐级放宽市值上限（看低市值是不是负担）")
    for cp, nm in [(0.2, "市值≤20%"), (0.3, "市值≤30%"), (0.4, "市值≤40%"),
                   (0.5, "市值≤50%"), (0.7, "市值≤70%"), (1.0, "不加市值约束")]:
        m = (df["turn_pct"] <= 0.3) & (df["cap_pct"] <= cp)
        print(f"  --- {nm} + 换手≤30% ---")
        row(df, m & tr, f"{nm} 训练", rows)
        row(df, m & oos, f"{nm} OOS", rows)

    print("\n[2] 纯换手（无市值约束）在不同换手档位的对比")
    for tp in (0.1, 0.2, 0.3, 0.4, 0.5):
        m = df["turn_pct"] <= tp
        row(df, m & tr, f"换手≤{int(tp*100)}% 训练", rows)
        row(df, m & oos, f"换手≤{int(tp*100)}% OOS", rows)

    print("\n[3] 双向：低换手 + 剔除高市值（下侧不做限制，只砍上侧）")
    for cp in (0.8, 0.9, 1.0):
        m = (df["turn_pct"] <= 0.3) & (df["cap_pct"] <= cp)
        row(df, m & oos, f"换手≤30% & 市值≤{int(cp*100)}% OOS", rows)

    print("\n[4] 最优候选的逐月稳定性")
    best = (df["turn_pct"] <= 0.3)
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    for mm in ["2026-07", "2026-08", "2026-09", "2026-10"]:
        row(df, best & (df["mon"] == mm), f"换手≤30% {mm[5:]}月", rows)

    print("\n[5] 与全市场对照（同期间，扣费净值）")
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        gm = df[seg].groupby("date")["r_oc"].mean()
        print(f"  {sl} 全市场: {len(gm)}日 日均毛{gm.mean():+.3f}% "
              f"扣费{(gm.mean()-COST):+.3f}% 净值{float((1+(gm-COST)/100).prod()):.4f}")

    pd.DataFrame(rows).to_csv(OUT / "c20i_sizecheck.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20i_sizecheck.csv'}")


if __name__ == "__main__":
    main()
