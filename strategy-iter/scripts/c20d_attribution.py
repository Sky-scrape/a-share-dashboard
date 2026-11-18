# -*- coding: utf-8 -*-
"""C20d：9-10 月真样本外的归因分解与净值口径。

要回答三个问题：
1. 「小市值+低换手」在 9-10 月超额只剩 +0.112%，是谁拖累的？（拆开看）
2. 低换手单独在样本外 t=2.55，它是真的还是被小市值带的？（分层正交检验）
3. 扣掉 0.15% 双边成本后，还有没有钱赚？（绝对值口径，不是超额口径）
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15          # 双边费用 %，沿用 C20 口径


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
    return {"桶": label, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
            "日级t": round(float(t), 2) if t == t else None, "天数": nd,
            "绝对%": round(float(s["r_oc"].mean()), 3),
            "扣费后绝对%": round(float(s["r_oc"].mean()) - COST, 3),
            "日胜率%": round(float((s.groupby("date")["r_oc"].mean() > 0).mean() * 100), 1),
            "个股胜率%": round(float((s["r_oc"] > 0).mean() * 100), 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<30s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% t={str(x['日级t']):>6s} "
              f"绝对{x['绝对%']:+7.3f}% 扣费{x['扣费后绝对%']:+7.3f}% "
              f"日胜率{x['日胜率%']:5.1f}% 股胜率{x['个股胜率%']:5.1f}%")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    tr = df[df["date"] <= TRAIN_END]
    oos = df["date"] > TRAIN_END
    turn_q = float(tr["turn"].median())

    rows = []
    def push(*a):
        for x in a:
            if x:
                rows.append(x)
                show(x)

    print("[A] 归因分解：谁贡献了「小市值+低换手」（阈值 7-8 月定）")
    print("  --- 训练 7-8 月 ---")
    push(row(df, df["date"] <= TRAIN_END, "训练 全市场"),
         row(df, (df["date"] <= TRAIN_END) & (df["cap"] < 30), "训练 小市值"),
         row(df, (df["date"] <= TRAIN_END) & (df["turn"] <= turn_q), "训练 低换手"),
         row(df, (df["date"] <= TRAIN_END) & (df["cap"] < 30) & (df["turn"] <= turn_q), "训练 小+低换"))
    print("  --- 样本外 9-10 月 ---")
    push(row(df, oos, "OOS 全市场"),
         row(df, oos & (df["cap"] < 30), "OOS 小市值"),
         row(df, oos & (df["turn"] <= turn_q), "OOS 低换手"),
         row(df, oos & (df["cap"] < 30) & (df["turn"] <= turn_q), "OOS 小+低换"))

    print("\n[B] 低换手 在市值分层内是否仍有效（OOS，去掉小市值混淆）")
    for lo, hi, nm in [(-np.inf, 30, "微盘<30亿"), (30, 60, "小盘30-60亿"),
                       (60, 150, "中盘60-150亿"), (150, np.inf, "大盘>150亿")]:
        seg = df[(df["cap"] > lo) & (df["cap"] <= hi)]
        push(row(seg, seg["date"] <= TRAIN_END, f"{nm} 全 | 训练"),
             row(seg, (seg["date"] <= TRAIN_END) & (seg["turn"] <= turn_q), f"{nm} 低换 | 训练"),
             row(seg, (seg["date"] > TRAIN_END), f"{nm} 全 | OOS"),
             row(seg, (seg["date"] > TRAIN_END) & (seg["turn"] <= turn_q), f"{nm} 低换 | OOS"))

    print("\n[C] 换手率五档单调性（阈值训练段，OOS 检验是否还单调）")
    qs = np.unique(tr["turn"].quantile(np.linspace(0, 1, 6)).values)
    lab = pd.cut(df["turn"], bins=qs, labels=False, include_lowest=True)
    for b in sorted(pd.unique(lab.dropna())):
        push(row(df, (lab == b) & (df["date"] <= TRAIN_END), f"换手 Q{int(b)+1} | 训练"),
             row(df, (lab == b) & oos, f"换手 Q{int(b)+1} | OOS"))

    print("\n[D] 「小+低换」逐月（看衰减路径）")
    for m in ["2026-07", "2026-08", "2026-09", "2026-10"]:
        s = df[(df["mon"] == m) & (df["cap"] < 30) & (df["turn"] <= turn_q)]
        push(row(df, (df["mon"] == m) & (df["cap"] < 30) & (df["turn"] <= turn_q),
                 f"小+低换 {m[-2:]}月"))

    print("\n[E] 扣费口径判断：能不能赚钱（不是能不能赢指数）")
    base_oos = df[oos]
    print(f"  9-10 月全市场可交易基线 {base_oos['r_oc'].mean():+.3f}%，"
          f"扣费 {base_oos['r_oc'].mean()-COST:+.3f}%")
    s = df[oos & (df["cap"] < 30) & (df["turn"] <= turn_q)]
    print(f"  小+低换 {s['r_oc'].mean():+.3f}% → 扣费 {s['r_oc'].mean()-COST:+.3f}%，"
          f"相比基线改善 {(s['r_oc'].mean()-base_oos['r_oc'].mean()):+.3f}pp")
    print(f"  → 结论：{'仍为负收益' if s['r_oc'].mean()-COST < 0 else '转为正收益'}")

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "c20d_attribution.csv",
                                                index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20d_attribution.csv'} ({len(rows)} 桶)")


if __name__ == "__main__":
    main()
