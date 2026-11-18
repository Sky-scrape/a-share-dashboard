# -*- coding: utf-8 -*-
"""C20h：分位口径下「低市值+低换手」的稳健性诊断。

上一轮 C20d/C20e 用绝对阈值（cap<30亿）+ 正交化，判了「未通过」。
换成每日横截面分位后，样本外超额反而更稳（t=2.11~2.86）。
必须先搞清楚哪个才是对的，再决定改不改结论。

三个必查项：
[1] 共线性诊断：正交化把 turn 对 ln(amount) 回归，若两者相关 >0.9，
    那不是「证伪」，是「过度剔除」——换手率本来就约等于成交额/流通市值。
[2] 分位组合的逐月路径：10 月是不是真的还正。
[3] 市值分层内的低换手（用分位，不用绝对阈值）：效应是否只在某一层。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15


def day_t(s):
    g = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(g) < 4:
        return np.nan, len(g)
    return g.mean() / (g.std(ddof=1) / np.sqrt(len(g))), len(g)


def row(df, mask, label, rows):
    s = df[mask]
    if len(s) < 80:
        return
    t, nd = day_t(s)
    gm = s.groupby("date")["r_oc"].mean()
    r = {"桶": label, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
         "日级t": round(float(t), 2) if t == t else None, "天数": nd,
         "绝对%": round(float(s["r_oc"].mean()), 3),
         "扣费%": round(float(s["r_oc"].mean()) - COST, 3),
         "净值": round(float((1 + (gm - COST) / 100).prod()), 4)}
    rows.append(r)
    print(f"  {r['桶']:<36s} n={r['n']:>6d} 超额{r['超额%']:+7.3f}% "
          f"t={str(r['日级t']):>6s}({r['天数']:>2d}日) 绝对{r['绝对%']:+7.3f}% "
          f"扣费{r['扣费%']:+7.3f}% 净值{r['净值']:.4f}")


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p[p["tradable"] == 1].copy()
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    g = df.groupby("date")
    df["cap_pct"] = g["cap"].rank(pct=True)
    df["turn_pct"] = g["turn"].rank(pct=True)
    df["amount_pct"] = g["amount"].rank(pct=True)
    df["atr_rank"] = g["atr_pct"].rank(pct=True)
    tr, oos = df["date"] <= TRAIN_END, df["date"] > TRAIN_END
    rows = []

    print("[1] 共线性诊断：turn_pct 与其它分位特征的相关（逐日中位）")
    for c in ("amount_pct", "cap_pct", "atr_rank"):
        cs = []
        for _, gg in df.groupby("date"):
            a, b = gg["turn_pct"].rank(), gg[c].rank()
            if a.std() > 0 and b.std() > 0:
                cs.append(a.corr(b))
        cs = pd.Series(cs)
        flag = "  ← 高度共线，正交化会过度剔除" if abs(cs.median()) > 0.8 else ""
        print(f"  turn_pct vs {c:<12s} 中位 {cs.median():+.3f}{flag}")
    print(f"  原始值 turn vs amount 逐日中位 "
          f"{_rc(df, 'turn', 'amount'):+.3f}")

    print("\n[2] 分位组合逐月路径（三档）")
    for cp, tp in [(0.2, 0.2), (0.3, 0.3), (0.5, 0.3)]:
        m = (df["cap_pct"] <= cp) & (df["turn_pct"] <= tp)
        print(f"  --- 市值≤{int(cp*100)}% + 换手≤{int(tp*100)}% ---")
        for mm in ["2026-07", "2026-08", "2026-09", "2026-10"]:
            row(df, m & (df["mon"] == mm), f"  {mm[5:]}月", rows)

    print("\n[3] 市值分层内看低换手（分位口径，OOS）")
    for lo, hi, nm in [(0, 0.2, "市值最低20%"), (0.2, 0.4, "市值20-40%"),
                       (0.4, 0.6, "市值40-60%"), (0.6, 0.8, "市值60-80%"),
                       (0.8, 1.0, "市值最高20%")]:
        seg = df[(df["cap_pct"] > lo) & (df["cap_pct"] <= hi)] if lo else df[df["cap_pct"] <= hi]
        row(df, (df["cap_pct"] > lo) & (df["cap_pct"] <= hi) & oos, f"{nm} 全 | OOS", rows)
        row(df, (df["cap_pct"] > lo) & (df["cap_pct"] <= hi) & oos
            & (df["turn_pct"] <= 0.3), f"{nm} 低换手≤30% | OOS", rows)

    print("\n[4] 换手率分位在「剔除成交额」后是否还活（对照组）")
    print("  说明：turn ≈ 成交额/流通市值，剔除成交额等于剔掉换手率本身，此检验只作参照")
    df["turn_ex_amt"] = np.nan
    for _, gg in df.groupby("date"):
        y = gg["turn_pct"].values
        X = np.column_stack([np.ones(len(gg)), gg["amount_pct"].values])
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < 100:
            continue
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        df.loc[gg.index[ok], "turn_ex_amt"] = (y - X @ b)[ok]
    for seg, sl in [(tr, "训练"), (oos, "OOS")]:
        sub = df[seg & df["turn_ex_amt"].notna()]
        thr = float(sub["turn_ex_amt"].quantile(0.2))
        row(df, (df["turn_ex_amt"] <= thr) & seg, f"剔除成交额后残差最低20% | {sl}", rows)

    print("\n[5] 结论口径对照：同一组合，绝对阈值 vs 分位")
    for nm, m in [("绝对 cap<30亿 + turn≤1.71%",
                   (df["cap"] < 30) & (df["turn"] <= 1.71)),
                  ("分位 cap≤30% + turn≤30%",
                   (df["cap_pct"] <= 0.3) & (df["turn_pct"] <= 0.3))]:
        row(df, m & tr, f"{nm} | 训练", rows)
        row(df, m & oos, f"{nm} | OOS", rows)

    pd.DataFrame(rows).to_csv(OUT / "c20h_diagnose.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20h_diagnose.csv'}")


def _rc(df, a, b):
    cs = []
    for _, gg in df.groupby("date"):
        x, y = gg[a].rank(), gg[b].rank()
        if len(gg) > 30 and x.std() > 0 and y.std() > 0:
            cs.append(x.corr(y))
    return float(pd.Series(cs).median()) if cs else np.nan


if __name__ == "__main__":
    main()
