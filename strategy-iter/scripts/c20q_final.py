# -*- coding: utf-8 -*-
"""C20q：最终配置确认 + 净值曲线，供写入报告与日志。

候选最终配置（三因子）：
  1. 市值 < 100 亿（绝对阈值，覆盖分位零漂移）
  2. 换手率每日横截面分位 ≤ 50%
  3. 下影线每日横截面分位 ≤ 30%（剔除振幅后仍显著）

同时给出：
- 三因子逐月路径与剔除单月稳健性
- 扣费净值曲线（逐日）
- 与基准池、全市场的对照
- 参数敏感性（换手 30/40/50/60，下影 20/30/40）
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15
COLS = ["date", "tradable", "r_oc", "cap", "turn", "amp", "lower", "chg"]


def load():
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    out = {}
    for c in [x for x in COLS if x in names]:
        col = pf.read(columns=[c]).column(0).combine_chunks()
        if c == "date":
            ts = pc.cast(col, "timestamp[s]")
            out[c] = pd.to_datetime(np.asarray(ts.cast("int64")) * 10**9)
        elif c == "tradable":
            out[c] = np.asarray(col.cast("int8"))
        else:
            out[c] = np.asarray(pc.cast(col, "float32"))
    return pd.DataFrame(out, copy=False)


def agg(df, m):
    s = df[m]
    if len(s) < 40:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    gm = s.groupby("date")["r_oc"].mean()
    return {"n": len(s), "日均": len(s) / len(gm), "超额": float(s["r_oc_x"].mean()),
            "t": float(gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))),
            "净值": float((1 + (gm - COST) / 100).prod()),
            "毛均": float(gm.mean()), "ser": gm}


def show(nm, a, b):
    if not a or not b:
        return
    print(f"  {nm:<28s} 训练{a['超额']:+6.3f}%(t={a['t']:5.2f}) "
          f"OOS{b['超额']:+6.3f}%(t={b['t']:5.2f}) 净值{b['净值']:.4f} 日均{b['日均']:>4.0f}")


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")
    df["amp_p"] = gg["amp"].rank(pct=True).astype("float32")
    df["month"] = df["date"].dt.month
    tr = df["date"] <= TRAIN_END
    oos = df["date"] > TRAIN_END

    base = (df["cap"] < 100) & (df["turn_pct"] <= 0.5)
    final = base & (df["lower_p"] <= 0.3)

    print("[1] 最终配置对照")
    show("全市场基线", agg(df, tr), agg(df, oos))
    show("基准池（市值+换手）", agg(df, base & tr), agg(df, base & oos))
    show("最终（+下影≤30%）", agg(df, final & tr), agg(df, final & oos))

    print("\n[2] 三因子逐月路径")
    print(f"  {'组合':<24s}{'7月':>9s}{'8月':>9s}{'9月':>9s}{'10月':>9s}{'正月':>5s}")
    for nm, m in [("基准池", base), ("最终三因子", final)]:
        vals = []
        for mo in (7, 8, 9, 10):
            s = df[m & (df["month"] == mo)]
            vals.append(s["r_oc_x"].mean() if len(s) > 30 else np.nan)
        pos = sum(1 for v in vals if v == v and v > 0)
        print(f"  {nm:<24s}" + "".join(
            f"{v:+9.3f}" if v == v else f"{'—':>9s}" for v in vals) + f"{pos:>4d}/4")

    print("\n[3] 剔除单月稳健性")
    for nm, m in [("基准池", base), ("最终三因子", final)]:
        a = agg(df, m & oos)
        if not a:
            continue
        line = f"  {nm:<24s} 全OOS t={a['t']:.2f}"
        for mo in (9, 10):
            keep = a["ser"][a["ser"].index.month != mo]
            t = keep.mean() / (keep.std(ddof=1) / np.sqrt(len(keep)))
            line += f" | 剔{mo}月 t={t:.2f}"
        print(line)

    print("\n[4] 参数敏感性（OOS 超额% / t）")
    print(f"  {'下影\\换手':<10s}" + "".join(f"{f'≤{t}%':>14s}" for t in (30, 40, 50, 60)))
    for lp in (0.2, 0.3, 0.4):
        line = f"  ≤{int(lp*100)}%      "
        for tp in (0.3, 0.4, 0.5, 0.6):
            b = agg(df, (df["cap"] < 100) & (df["turn_pct"] <= tp)
                    & (df["lower_p"] <= lp) & oos)
            line += f"{b['超额']:+7.3f}/{b['t']:5.2f}" if b else f"{'—':>14s}"
        print(line)

    print("\n[5] 扣费净值曲线（最终配置 vs 全市场）")
    sa, sb = agg(df, final & oos), agg(df, oos)
    if sa and sb:
        cum_a = (1 + (sa["ser"] - COST) / 100).cumprod()
        cum_b = (1 + (sb["ser"] - COST) / 100).cumprod()
        print(f"  {'日期':<12s}{'最终配置':>12s}{'全市场':>12s}")
        for i in range(0, len(cum_a), max(1, len(cum_a) // 8)):
            d = cum_a.index[i]
            print(f"  {d.strftime('%Y-%m-%d'):<12s}{cum_a.iloc[i]:>12.4f}"
                  f"{cum_b.loc[d]:>12.4f}")
        print(f"  {'期末':<12s}{cum_a.iloc[-1]:>12.4f}{cum_b.iloc[-1]:>12.4f}")

    print("\n[6] 最终数字")
    a, b = agg(df, final & tr), agg(df, final & oos)
    print(f"  训练 7-8月: 超额 {a['超额']:+.3f}% t={a['t']:.2f} 净值 {a['净值']:.4f} "
          f"日均 {a['日均']:.0f} 只")
    print(f"  样本 9-10月: 超额 {b['超额']:+.3f}% t={b['t']:.2f} 净值 {b['净值']:.4f} "
          f"日均 {b['日均']:.0f} 只 日均毛 {b['毛均']:+.3f}%")
    print(f"  扣费后日均 {(b['毛均']-COST):+.3f}%  对比全市场 "
          f"{agg(df,oos)['净值']:.4f}")


if __name__ == "__main__":
    main()
