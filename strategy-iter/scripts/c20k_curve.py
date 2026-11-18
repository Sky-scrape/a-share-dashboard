# -*- coding: utf-8 -*-
"""C20k：为什么 100 亿比 30 亿好，以及 t 值峰值在哪。

C20j 结果：
  cap<30亿 + 换手≤30%  → OOS 超额 +0.118%, t=1.54
  cap<100亿 + 换手≤30% → OOS 超额 +0.170%, t=2.66   ← t 最高
  cap<150亿 + 换手≤30% → OOS 超额 +0.178%, t=2.67
  不加市值            → OOS 超额 +0.203%, t=2.09

但净值反过来：100亿 0.8354 < 150亿 0.8382 < 不加 0.8460。
t 和净值背离，说明「收紧市值」提高的是「相对稳定性」，代价是「绝对抗跌性」。
这里把两件事分开量化，并做：
[1] 阈值分位稳定性扫描（哪个绝对市值阈值的分位覆盖最稳）
[2] t 值随市值阈值的完整曲线，找峰值与平台
[3] 换手档 × 市值档 的 t 网格
[4] 剔除最差月后的稳健性
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def stats(df, mask):
    s = df[mask]
    if len(s) < 80:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    t = gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))
    gm = s.groupby("date")["r_oc"].mean()
    return {"n": len(s), "日均": len(s) / len(gm),
            "超额": float(s["r_oc_x"].mean()), "t": float(t),
            "绝对": float(s["r_oc"].mean()),
            "净值": float((1 + (gm - COST) / 100).prod()),
            "std": float(gm.std(ddof=1))}


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["turn_pct"] = df.groupby("date")["turn"].rank(pct=True)
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    tr, oos = df["date"] <= TRAIN_END, df["date"] > TRAIN_END
    rows = []

    print("[1] 阈值分位稳定性：哪个绝对市值阈值的「覆盖分位」最稳")
    print(f"  {'阈值':<10s}{'训练覆盖':>10s}{'OOS覆盖':>10s}{'漂移':>10s}  判定")
    for c in (20, 30, 50, 80, 100, 120, 150, 200, 300):
        a = float((df[tr]["cap"] <= c).groupby(df[tr]["date"]).mean().median())
        b = float((df[oos]["cap"] <= c).groupby(df[oos]["date"]).mean().median())
        d = abs(a - b) * 100
        flag = "← 最稳" if d < 0.5 else ("稳定" if d < 2 else "明显漂移")
        print(f"  {c:>4d}亿{'':<4s}{a*100:9.1f}%{b*100:9.1f}%{d:9.1f}pp  {flag}")
        rows.append({"桶": f"阈值稳定性 {c}亿", "n": 0, "日均只数": 0,
                     "超额%": round(b * 100, 2), "日级t": None, "天数": 0,
                     "绝对%": round(d, 2), "扣费%": None, "净值": None, "日胜率%": None})

    print("\n[2] t 值随市值阈值的完整曲线（换手≤30%，OOS）")
    print(f"  {'阈值':<12s}{'n':>7s}{'日均':>6s}{'超额%':>9s}{'t':>7s}{'净值':>8s}{'日波动':>8s}")
    best = None
    for c in (20, 30, 40, 50, 60, 80, 100, 120, 150, 200, 300, 500, 1e9):
        st = stats(df, (df["cap"] < c) & (df["turn_pct"] <= 0.3) & oos)
        if not st:
            continue
        nm = f"<{c:.0f}亿" if c < 1e9 else "不限"
        print(f"  {nm:<12s}{st['n']:>7d}{st['日均']:>6.0f}{st['超额']:>8.3f}%"
              f"{st['t']:>7.2f}{st['净值']:>8.4f}{st['std']:>8.2f}")
        if best is None or st["t"] > best[1]:
            best = (nm, st["t"], st)
    print(f"  → t 值峰值在 {best[0]}（t={best[1]:.2f}）")

    print("\n[3] 换手档 × 市值档 的 OOS t 网格")
    caps = [30, 50, 100, 150, 300, 1e9]
    turns = [0.1, 0.2, 0.3, 0.5]
    hdr = "  换手\\市值  " + "".join(f"{('<' + str(int(c)) + '亿') if c < 1e9 else '不限':>10s}" for c in caps)
    print(hdr)
    for tp in turns:
        line = f"  ≤{int(tp*100):>3d}%     "
        for c in caps:
            st = stats(df, (df["cap"] < c) & (df["turn_pct"] <= tp) & oos)
            line += f"{(f'{st[chr(116)]:.2f}' if st else '-'):>10s}"
        print(line)

    print("\n[4] 稳健性：剔除最差单月后的 OOS t")
    m = (df["cap"] < 100) & (df["turn_pct"] <= 0.3)
    sub = df[m & oos].copy()
    months = sorted(sub["mon"].unique())
    gm_all = sub.groupby("date")["r_oc_x"].mean()
    t_all = gm_all.mean() / (gm_all.std(ddof=1) / np.sqrt(len(gm_all)))
    print(f"  全 OOS: {len(gm_all)}日 t={t_all:.2f}")
    for mm in months:
        keep = gm_all[gm_all.index.map(lambda d: d.strftime("%Y-%m") != mm)]
        t = keep.mean() / (keep.std(ddof=1) / np.sqrt(len(keep)))
        print(f"  剔除 {mm}: {len(keep)}日 t={t:.2f}  "
              f"({'仍显著' if t > 1.96 else '掉出显著'})")

    print("\n[5] 关键对照：三个候选的完整画像（OOS）")
    for nm, m_ in [("cap<30亿 + 换手≤30%", (df["cap"] < 30) & (df["turn_pct"] <= 0.3)),
                   ("cap<100亿 + 换手≤30%", (df["cap"] < 100) & (df["turn_pct"] <= 0.3)),
                   ("换手≤30%（不限市值）", df["turn_pct"] <= 0.3),
                   ("全市场基线", pd.Series(True, index=df.index))]:
        st = stats(df, m_ & oos)
        print(f"  {nm:<24s} n={st['n']:>6d}(日均{st['日均']:>4.0f}) "
              f"超额{st['超额']:+6.3f}% t={st['t']:>5.2f} "
              f"绝对{st['绝对']:+6.3f}% 净值{st['净值']:.4f}")

    pd.DataFrame(rows).to_csv(OUT / "c20k_curve.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20k_curve.csv'}")


if __name__ == "__main__":
    main()
