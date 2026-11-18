# -*- coding: utf-8 -*-
"""C20p：下影因子的决定性检验——它是独立信号，还是振幅的另一种写法？

c20o 已排除涨跌停伪影（涨幅>9% 占 0.0%），且剔单月后 t=2.64/3.17 仍显著。
剩下最后一个致命疑点：**振幅极小的票，下影必然接近 0**（分母小、影线被压缩）。
若如此，「低下影」只是「低振幅」的机械重复，不是新信息。

检验：
[1] 下影与振幅的相关，以及「低下影」中有多少同时是「低振幅」
[2] 对振幅正交化后，下影残差还有没有超额
[3] 在「振幅已筛过」的子集里再加下影，增量是多少
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15
COLS = ["date", "tradable", "r_oc", "cap", "turn", "amp", "lower", "atr_pct", "chg"]


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
    if len(s) < 60:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    gm = s.groupby("date")["r_oc"].mean()
    return {"n": len(s), "日均": len(s) / len(gm), "超额": float(s["r_oc_x"].mean()),
            "t": float(gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))),
            "净值": float((1 + (gm - COST) / 100).prod()), "ser": gx}


def show(nm, a, b):
    if not a or not b:
        return
    print(f"  {nm:<26s} 训练{a['超额']:+.3f}%(t={a['t']:5.2f}) "
          f"OOS{b['超额']:+.3f}%(t={b['t']:5.2f}) 净值{b['净值']:.4f} 日均{b['日均']:.0f}")


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["amp_p"] = gg["amp"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")

    base = (df["cap"] < 100) & (df["turn_pct"] <= 0.5)
    tr = df["date"] <= TRAIN_END
    oos = df["date"] > TRAIN_END

    print("[1] 下影与振幅的关系")
    cs = []
    for _, g in df[base].groupby("date"):
        a, b = g["lower_p"].rank(), g["amp_p"].rank()
        if a.std() > 0 and b.std() > 0:
            cs.append(a.corr(b))
    print(f"  lower_p vs amp_p 逐日 rank 相关中位 = {float(pd.Series(cs).median()):+.3f}")

    lo_low = base & (df["lower_p"] <= 0.3)
    lo_amp = base & (df["amp_p"] <= 0.3)
    inter = (lo_low & lo_amp).sum()
    print(f"  「下影最低30%」n={lo_low.sum()}  「振幅最低30%」n={lo_amp.sum()}  "
          f"交集 n={inter}（占低下影 {inter/lo_low.sum()*100:.1f}%）")
    print(f"  低下影组的振幅分位中位 = {df.loc[lo_low,'amp_p'].median():.3f} "
          f"（基准池 {df.loc[base,'amp_p'].median():.3f}）")

    print("\n[2] 对振幅正交化后，下影残差是否还有超额（决定性检验）")
    df["res"] = np.nan
    for _, g in df[base].groupby("date"):
        y_all = g["lower_p"].values.astype("float64")
        x_all = g["amp_p"].values.astype("float64")
        ok = np.isfinite(y_all) & np.isfinite(x_all)
        if ok.sum() < 100:
            continue
        y, x = y_all[ok], x_all[ok]
        X = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        df.loc[g.index[ok], "res"] = y - X @ beta
    sub = df[base & df["res"].notna()]
    thr = float(sub["res"].quantile(0.3))
    show("下影残差最低30%", agg(df, (df["res"] <= thr) & tr),
         agg(df, (df["res"] <= thr) & oos))
    show("下影原值最低30%", agg(df, lo_low & tr), agg(df, lo_low & oos))
    show("振幅最低30%", agg(df, lo_amp & tr), agg(df, lo_amp & oos))
    show("基准池", agg(df, base & tr), agg(df, base & oos))

    print("\n[3] 在「振幅已筛过」的子集里再加下影，增量多少")
    for nm, m in [("振幅≤30% 单独", lo_amp),
                  ("振幅≤30% + 下影≤30%", lo_amp & (df["lower_p"] <= 0.3)),
                  ("振幅≤30% + 下影≥70%", lo_amp & (df["lower_p"] >= 0.7))]:
        show(nm, agg(df, m & tr), agg(df, m & oos))

    print("\n[4] 反向核对：振幅≤30% 内部，下影高低的差距")
    a = df[lo_amp & (df["lower_p"] <= 0.3) & oos]
    b = df[lo_amp & (df["lower_p"] >= 0.7) & oos]
    if len(a) > 60 and len(b) > 60:
        print(f"  低下影 OOS {a['r_oc_x'].mean():+.3f}% (n={len(a)}) vs "
              f"高下影 {b['r_oc_x'].mean():+.3f}% (n={len(b)}) "
              f"差 {(a['r_oc_x'].mean()-b['r_oc_x'].mean()):+.3f}pp")

    print("\n[5] 判断")
    r = df[base & df["res"].notna()]
    res_t = agg(df, (df["res"] <= thr) & oos)
    if res_t and res_t["t"] > 1.96 and res_t["超额"] > 0.17:
        print("  下影在剔除振幅后仍有增量 → 可作为候选，但需 OOS 扩充验证")
    else:
        print("  下影在剔除振幅后增量消失 → 它是振幅的另一种写法，不加入")


if __name__ == "__main__":
    main()
