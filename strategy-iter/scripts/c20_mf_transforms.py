# -*- coding: utf-8 -*-
"""C20c：主力资金流的三种非线性变换（持续性 / 自身 z 分数 / 与已知负面因子叠加）。

单日水平已经证明无效（C20b：逐日 rank 相关中位 -0.004~-0.027，分档不单调，两段翻号）。
这里再给资金流留三次机会，避免「只测了最简单的用法就否掉」。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"


def load():
    df = pd.read_parquet(OUT / "panel_mf.parquet")
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["tradable"] == 1].copy()
    df = df[df["r_oc"].notna() & df["mf"].notna()].copy()
    df["mf_r"] = df["mf"] / df["amount"] * 100
    df = df.sort_values(["thscode", "date"])
    g = df.groupby("thscode")["mf_r"]
    df["mf_sum5"] = g.transform(lambda s: s.rolling(5, min_periods=3).sum())
    df["mf_mean20"] = g.transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["mf_std20"] = g.transform(lambda s: s.rolling(20, min_periods=10).std())
    df["mf_z"] = (df["mf_r"] - df["mf_mean20"]) / df["mf_std20"].replace(0, np.nan)
    df["mf_pos5"] = g.transform(lambda s: (s > 0).rolling(5, min_periods=3).sum())
    for c in ("r_oc",):
        df[c + "_x"] = df[c] - df.groupby("date")[c].transform("mean")
    dates = np.sort(df["date"].unique())
    cut = dates[len(dates) // 2]
    df["seg"] = np.where(df["date"] < cut, "train", "test")
    return df


def ex(df, mask, label):
    s = df[mask]
    s = s[s["r_oc_x"].notna()]
    if len(s) < 300:
        return None
    g = s.groupby("date")["r_oc_x"].mean()
    t = g.mean() / (g.std(ddof=1) / np.sqrt(len(g))) if len(g) > 2 else np.nan
    return {"桶": label, "n": len(s), "超额%": round(s["r_oc_x"].mean(), 3),
            "日级t": round(t, 2) if t == t else None, "天数": len(g),
            "胜率%": round((s["r_oc"] > 0).mean() * 100, 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<30s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% "
              f"t={str(x['日级t']):>5s}({x['天数']}日) 胜率{x['胜率%']:5.1f}%")


def main():
    df = load()
    dtr = df[df.seg == "train"]
    rows = []

    def both(mask, nm):
        for seg, lb in [("train", "训"), ("test", "验")]:
            x = ex(df, mask & (df.seg == seg), f"{nm} {lb}")
            if x:
                rows.append(x)
                show(x)

    print("[A] 持续性：近 5 日累计主力净额占比（训练段分位五档）")
    col = "mf_sum5"
    sub = df[df[col].notna()]
    qs = np.unique(sub[sub.seg == "train"][col].quantile(np.linspace(0, 1, 6)).values)
    lab = pd.cut(sub[col], bins=qs, labels=False, include_lowest=True)
    for b in sorted(pd.unique(lab.dropna())):
        both(lab == b, f"5日累计 Q{int(b)+1}")

    print("\n[B] 自身 20 日 z 分数（衡量是否异常放量进场）")
    sub = df[df["mf_z"].notna()]
    qs = np.unique(sub[sub.seg == "train"]["mf_z"].quantile([0.1, 0.3, 0.7, 0.9]).values)
    lab = pd.cut(sub["mf_z"], bins=[-np.inf, *qs, np.inf], labels=False)
    for b in sorted(pd.unique(lab.dropna())):
        both(lab == b, f"主力z分档 L{int(b)+1}")
    for nm, m in [("z>+2 (异常放量买)", sub["mf_z"] > 2), ("z<-2 (异常放量卖)", sub["mf_z"] < -2)]:
        both(m, nm)

    print("\n[C] 连续净流入天数")
    for k in (3, 4, 5):
        both(df["mf_pos5"] >= k, f"近5日≥{k}日净流入")

    print("\n[D] 与已知负面因子叠加（检验能否加强而非替代）")
    dtr_local = df[df.seg == "train"]
    for nm, mask in [
        ("高换手(训中位以上)", df["turn"] > dtr_local["turn"].median()),
        ("20日涨幅前10%(训)", df["ret20"] >= dtr_local["ret20"].quantile(0.9)),
        ("成交额前10%(训)", df["amount"] >= dtr_local["amount"].quantile(0.9)),
    ]:
        both(mask & (df["mf"] < 0), f"{nm} + 主力净流出")
        both(mask & (df["mf"] > 0), f"{nm} + 主力净流入")

    print("\n[E] 小市值 + 低换手 + 主力流入（用户偏好组合）")
    tq = dtr_local["turn"].median()
    base_small = df["cap"] < 30
    both(base_small, "小市值")
    both(base_small & (df["turn"] <= tq), "小市值+低换手")
    both(base_small & (df["turn"] <= tq) & (df["mf"] > 0), "小市值+低换手+主力流入")
    both(base_small & (df["turn"] <= tq) & (df["mf"] < 0), "小市值+低换手+主力流出")

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "mf_transforms.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'mf_transforms.csv'}")


if __name__ == "__main__":
    main()
