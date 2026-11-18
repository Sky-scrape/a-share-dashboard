# -*- coding: utf-8 -*-
"""C20b：主力资金流的「同日内超额」检验。

为什么必须做同日内去均值：
7 月可交易全体 -0.388%、8 月 +0.196%，两段基线差 0.58pp。
直接比较分档绝对收益，等于把市场风格差异错算成因子效应（C15/C16 踩过的坑）。
做法：每个交易日把 r_oc 减去当日全体均值 → 得到当日超额，再按桶汇总。
日级 t：先按日求桶内均值，再对「桶 - 当日全体」这一差值序列做 t 检验。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
PANEL = OUT / "panel_mf.parquet"


def load():
    df = pd.read_parquet(PANEL)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["tradable"] == 1].copy()
    df = df[df["r_oc"].notna() & df["mf"].notna()].copy()
    df["mf_r"] = df["mf"] / df["amount"] * 100
    for c in ("r_oc", "overnight", "intraday"):
        df[c + "_x"] = df[c] - df.groupby("date")[c].transform("mean")
    dates = np.sort(df["date"].unique())
    cut = dates[len(dates) // 2]
    df["seg"] = np.where(df["date"] < cut, "train", "test")
    return df, cut


def excess(df, mask, label):
    """返回桶内同日内超额 + 日级 t。"""
    s = df[mask]
    if len(s) < 200:
        return None
    g = s.groupby("date")["r_oc_x"].mean()
    n_days = len(g)
    t = g.mean() / (g.std(ddof=1) / np.sqrt(n_days)) if n_days > 2 else np.nan
    g2 = s.groupby("date")["overnight_x"].mean()
    return {"桶": label, "n": len(s), "超额%": round(s["r_oc_x"].mean(), 3),
            "日级t": round(t, 2) if t == t else None, "天数": n_days,
            "隔夜超额%": round(s["overnight_x"].mean(), 3),
            "日内超额%": round(s["intraday_x"].mean(), 3),
            "胜率%": round((s["r_oc"] > 0).mean() * 100, 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<26s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% "
              f"t={str(x['日级t']):>5s}({x['天数']}日) 隔夜{x['隔夜超额%']:+6.2f} "
              f"日内{x['日内超额%']:+6.2f} 胜率{x['胜率%']:5.1f}%")


def main():
    df, cut = load()
    dtr = df[df.seg == "train"]
    print(f"可交易 {len(df)} 行 / {df['date'].nunique()} 天；切点 {pd.Timestamp(cut).date()}")
    print(f"  训练 {dtr['date'].nunique()} 天  验证 {df[df.seg=='test']['date'].nunique()} 天")

    rows = []

    print("\n[A] 主力净额占成交额 五档（阈值取训练段分位）")
    qs = np.unique(dtr["mf_r"].quantile(np.linspace(0, 1, 6)).values)
    lab = pd.cut(df["mf_r"], bins=qs, labels=False, include_lowest=True)
    for b in sorted(pd.unique(lab.dropna())):
        m = lab == b
        for seg, lb in [("train", "训"), ("test", "验")]:
            x = excess(df, m & (df.seg == seg), f"主力占比 Q{int(b)+1} {lb}")
            if x:
                rows.append(x)
                show(x)

    print("\n[B] 方向二分")
    for side, m in [("净流入", df.mf > 0), ("净流出", df.mf < 0)]:
        for seg, lb in [("train", "训"), ("test", "验")]:
            x = excess(df, m & (df.seg == seg), f"主力{side} {lb}")
            if x:
                rows.append(x)
                show(x)

    print("\n[C] 极端组（训练段 10/90 分位定阈）")
    hi, lo = dtr["mf_r"].quantile(0.9), dtr["mf_r"].quantile(0.1)
    for nm, m in [("主力流入Top10%", df.mf_r >= hi), ("主力流出Bot10%", df.mf_r <= lo)]:
        for seg, lb in [("train", "训"), ("test", "验")]:
            x = excess(df, m & (df.seg == seg), f"{nm} {lb}")
            if x:
                rows.append(x)
                show(x)

    print("\n[D] 小市值 <30亿 × 主力方向")
    small = df[df.cap < 30]
    for nm, m in [("小市值 全", pd.Series(True, index=df.index)),
                  ("小市值+主力流入", df.mf > 0), ("小市值+主力流出", df.mf < 0)]:
        mm = m & (df.cap < 30)
        for seg, lb in [("train", "训"), ("test", "验")]:
            x = excess(df, mm & (df.seg == seg), f"{nm} {lb}")
            if x:
                rows.append(x)
                show(x)

    print("\n[E] 主力方向 × 当日涨幅档（检验「流入=已涨过」的机制）")
    df["chg_q"] = pd.cut(df["chg"], bins=[-20, -2, 0, 3, 6, 9.5, 30], labels=False)
    for b in sorted(pd.unique(df["chg_q"].dropna())):
        m = df.chg_q == b
        s_in, s_out = df[m & (df.mf > 0)], df[m & (df.mf < 0)]
        print(f"  当日涨幅档 Q{int(b)+1}: 主力流入 n={len(s_in)} 超额{s_in['r_oc_x'].mean():+.3f}% | "
              f"流出 n={len(s_out)} 超额{s_out['r_oc_x'].mean():+.3f}% | "
              f"差 {s_in['r_oc_x'].mean()-s_out['r_oc_x'].mean():+.3f}pp")

    print("\n[F] 相关性（逐日 rank 相关，避免依赖 scipy）")
    for col in ("mf_r", "mf"):
        cs = []
        for _, g in df.groupby("date"):
            a, b = g[col].rank(), g["r_oc"].rank()
            if len(g) > 30 and a.std() > 0 and b.std() > 0:
                cs.append(a.corr(b))
        cs = pd.Series(cs)
        print(f"  {col} vs r_oc 逐日 spearman 中位 {cs.median():+.3f}, 均值 {cs.mean():+.3f}, "
              f"为负 {(cs < 0).sum()}/{len(cs)} 天")

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "mf_excess.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'mf_excess.csv'}")


if __name__ == "__main__":
    main()
