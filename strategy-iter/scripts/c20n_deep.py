# -*- coding: utf-8 -*-
"""C20n：对边缘候选做深度检验——它们是新因子，还是换手率的另一种说法？

c20m 全扫 29 个，严格判据 0 通过。但有几个候选值得单独查：
  20日均换手  OOS差 +0.409pp t=1.84
  60日累计换手 OOS差 +0.250pp t=1.37
  ATR占比     OOS差 +0.221pp t=1.17
  振幅        OOS差 +0.192pp t=1.60
  60日涨幅    OOS差 +0.147pp t=1.25 月正4/4

三件事：
[1] 共线性：这些候选与「换手分位」的相关有多高（>0.6 就是同义）
[2] 增量：在 base 池内再加一层筛选，超额能提升多少；提升是否显著
[3] 正交化：剔除换手后，候选自己还剩多少（这才是「新因子」的定义）
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15

COLS = ["date", "tradable", "r_oc", "cap", "turn", "amount", "volr",
        "amt_rank60", "turn_ma5", "turn_ma20", "turn_cum60", "turn_std60",
        "pos60", "ret20", "ret60", "dev60", "atr_pct", "amp", "lower", "body",
        "cost_disp", "profit60", "rel_str", "chg", "to_hi60"]

CAND = {"turn_ma20": "20日均换手", "turn_cum60": "60日累计换手",
        "atr_pct": "ATR占比", "amp": "振幅", "ret60": "60日涨幅",
        "dev60": "60日偏离", "pos60": "60日位置", "lower": "下影"}


def load():
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    keep = [c for c in COLS if c in names]
    out = {}
    for c in keep:
        s = pf.read(columns=[c]).column(0).to_pandas()
        if c == "date":
            out[c] = pd.to_datetime(s.to_numpy())
        else:
            out[c] = pd.to_numeric(np.asarray(s), errors="coerce").astype("float32")
        del s
    return pd.DataFrame(out, copy=False)


def agg(s):
    if len(s) < 60:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    gm = s.groupby("date")["r_oc"].mean()
    return {"n": len(s), "日均": len(s) / len(gm), "超额": float(s["r_oc_x"].mean()),
            "t": float(gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))),
            "净值": float((1 + (gm - COST) / 100).prod())}


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    for c in CAND:
        df[c + "_p"] = gg[c].rank(pct=True).astype("float32")

    base = (df["cap"] < 100) & (df["turn_pct"] <= 0.5)
    tr = df["date"] <= TRAIN_END
    oos = df["date"] > TRAIN_END
    b_tr, b_oos = agg(df[base & tr]), agg(df[base & oos])
    print(f"[0] 基准池 训练超额{b_tr['超额']:+.3f}% t={b_tr['t']:.2f} | "
          f"OOS超额{b_oos['超额']:+.3f}% t={b_oos['t']:.2f} 净值{b_oos['净值']:.4f}")

    print("\n[1] 候选与「换手分位」的共线性（逐日 rank 相关中位）")
    print(f"  {'候选':<14s}{'vs 换手分位':>13s}  判定")
    for c, nm in CAND.items():
        cs = []
        for _, g in df[base].groupby("date"):
            a, b = g[c + "_p"].rank(), g["turn_pct"].rank()
            if len(g) > 100 and a.std() > 0 and b.std() > 0:
                cs.append(a.corr(b))
        rho = float(pd.Series(cs).median())
        flag = "同义（不能算新因子）" if abs(rho) > 0.6 else (
            "高度相关" if abs(rho) > 0.4 else "较独立")
        print(f"  {nm:<14s}{rho:>13.3f}  {flag}")

    print("\n[2] 在 base 池内再加一层筛选（取该因子最低 30%）")
    print(f"  {'候选':<14s}{'训练超额':>10s}{'t':>6s}{'OOS超额':>10s}{'t':>6s}"
          f"{'OOS净值':>9s}{'日均':>6s}{'增量pp':>9s}")
    for c, nm in CAND.items():
        m = base & (df[c + "_p"] <= 0.3)
        a, b = agg(df[m & tr]), agg(df[m & oos])
        if not a or not b:
            continue
        print(f"  {nm:<14s}{a['超额']:>10.3f}{a['t']:>6.2f}{b['超额']:>10.3f}"
              f"{b['t']:>6.2f}{b['净值']:>9.4f}{b['日均']:>6.0f}"
              f"{(b['超额']-b_oos['超额']):>9.3f}")

    print("\n[3] 正交化：在 base 池内，把候选对换手分位回归取残差，看残差还有没有超额")
    print("    （这才是判断「是否独立因子」的正确做法）")
    print(f"  {'候选':<14s}{'残差低20% 训练':>15s}{'t':>6s}{'残差低20% OOS':>15s}"
          f"{'t':>6s}{'日均':>6s}")
    for c, nm in CAND.items():
        df["res"] = np.nan
        for _, g in df[base].groupby("date"):
            y = g[c + "_p"].values
            x = g["turn_pct"].values
            ok = np.isfinite(y) & np.isfinite(x)
            if ok.sum() < 100:
                continue
            X = np.column_stack([np.ones(ok.sum()), x[ok]])
            beta, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
            df.loc[g.index[ok], "res"] = y[ok] - X @ beta
        sub = df[base & df["res"].notna()]
        thr = float(sub["res"].quantile(0.2))
        m = base & (df["res"] <= thr)
        a, b = agg(df[m & tr]), agg(df[m & oos])
        if not a or not b:
            continue
        print(f"  {nm:<14s}{a['超额']:>15.3f}{a['t']:>6.2f}{b['超额']:>15.3f}"
              f"{b['t']:>6.2f}{b['日均']:>6.0f}")

    print("\n[4] 双因子叠加（换手分位≤50% + 候选≤30%），只列 OOS 超额正增量的")
    for c, nm in CAND.items():
        m = base & (df[c + "_p"] <= 0.3)
        a, b = agg(df[m & tr]), agg(df[m & oos])
        if not a or not b:
            continue
        if b["超额"] > b_oos["超额"] and a["超额"] > b_tr["超额"]:
            print(f"  ★ {nm}: 训练 {b_tr['超额']:.3f}→{a['超额']:.3f} "
                  f"OOS {b_oos['超额']:.3f}→{b['超额']:.3f} "
                  f"(+{(b['超额']-b_oos['超额']):.3f}pp) t={b['t']:.2f} 日均{b['日均']:.0f}")

    print("\n[5] 结论数据：三个最强的「低活跃度」类因子彼此相关度")
    for a_, b_ in [("atr_pct_p", "amp_p"), ("atr_pct_p", "turn_pct"), ("amp_p", "turn_pct")]:
        cs = []
        for _, g in df[base].groupby("date"):
            x, y = g[a_].rank(), g[b_].rank()
            if x.std() > 0 and y.std() > 0:
                cs.append(x.corr(y))
        print(f"  {a_:<12s} vs {b_:<12s} {float(pd.Series(cs).median()):+.3f}")


if __name__ == "__main__":
    main()
