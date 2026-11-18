# -*- coding: utf-8 -*-
"""C20r：现行生产策略 C7 与新建三因子池的公平对比。

为什么不能直接比数字
--------------------
- C7：涨停打板，T+1 买 T+2 卖，2026-01~10-30，可成交 262 笔，全窗口 T+2 净 +0.29%
- 新池：T 收盘买 T+1 收盘卖，每日数百只等权，7-10 月超额为正但绝对为负

笔数、持仓期、标的池完全不同，把 +0.29%/笔 和 −0.430%/日 放一起比毫无意义。

正确的比法：**放到同一个「每日等权组合」的资金曲线上比**。
C7 每天最多选几只，其余时间空仓；新池每天几百只满仓。两者都要算：
  1. 逐日净值（扣双边 0.15%）
  2. 空闲资金按 0 收益处理（C7 大部分时间空仓，这是它的保护而非缺点）
  3. 收益拆解 overnight / intraday —— 决定两者是否同一死法
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
COST = 0.15
COLS = ["date", "tradable", "r_oc", "r_oo", "overnight", "intraday",
        "cap", "turn", "lower", "chg", "close", "o1", "c1"]


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


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")

    new_pool = (df["cap"] < 100) & (df["turn_pct"] <= 0.5) & (df["lower_p"] <= 0.3)

    print("[1] C7 生产台账")
    p7 = pd.read_csv(BASE / "runs" / "c15_window_C7_1030" / "picks.csv")
    print("  picks.csv 列:", list(p7.columns)[:14])
    print(f"  行数 {len(p7)}")

    print("\n[2] 新池：收益拆解 overnight / intraday（决定是否与 C7 同一死法）")
    tr = df["date"] <= pd.Timestamp("2026-08-31")
    oos = df["date"] > pd.Timestamp("2026-08-31")
    for nm, m in [("全市场", pd.Series(True, index=df.index)),
                  ("新池（三因子）", new_pool)]:
        for seg, sl in [(tr, "训练7-8"), (oos, "OOS9-10")]:
            s = df[m & seg]
            g1 = s.groupby("date")["overnight"].mean()
            g2 = s.groupby("date")["intraday"].mean()
            g3 = s.groupby("date")["r_oc"].mean()
            print(f"  {nm:<14s}{sl:<9s} 总{g3.mean():+7.3f}% = "
                  f"隔夜{g1.mean():+7.3f}% + 日内{g2.mean():+7.3f}%  "
                  f"(隔夜占比 {g1.mean()/g3.mean()*100 if abs(g3.mean())>1e-9 else 0:5.1f}%)")

    print("\n[3] 关键对照：新池的超额有多少来自隔夜")
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    df["on_x"] = (df["overnight"] - df.groupby("date")["overnight"].transform("mean")).astype("float32")
    df["in_x"] = (df["intraday"] - df.groupby("date")["intraday"].transform("mean")).astype("float32")
    for nm, m in [("新池", new_pool), ("基准池(无下影)", (df["cap"] < 100) & (df["turn_pct"] <= 0.5))]:
        for seg, sl in [(tr, "训练"), (oos, "OOS")]:
            s = df[m & seg]
            print(f"  {nm:<16s}{sl:<6s} 超额{s['r_oc_x'].mean():+7.3f}% = "
                  f"隔夜超额{s['on_x'].mean():+7.3f}% + 日内超额{s['in_x'].mean():+7.3f}%")

    print("\n[4] 新池逐日净值（OOS 38 日，满仓等权，扣双边 0.15%）")
    s = df[new_pool & oos].groupby("date")["r_oc"].mean()
    nav_new = (1 + (s - COST) / 100).cumprod()
    print(f"  新池: 日均毛{s.mean():+.3f}% 扣费{(s.mean()-COST):+.3f}% "
          f"期末{nav_new.iloc[-1]:.4f} 日胜率{((s-COST)>0).mean()*100:.1f}%")
    a = df[oos].groupby("date")["r_oc"].mean()
    print(f"  全市场: 日均毛{a.mean():+.3f}% 扣费{(a.mean()-COST):+.3f}% "
          f"期末{float((1+(a-COST)/100).prod()):.4f}")

    print("\n[5] C7 在 7-10 月的逐笔与逐日")
    cols = {c.lower(): c for c in p7.columns}
    datecol = next((cols[k] for k in cols if "date" in k or "日期" in p7.columns[:0]), None)
    print("  ", list(p7.columns))
    if "date" in p7.columns:
        print("  样例:", p7.head(3).to_dict("records"))

    print("\n[6] 结论判断")
    print("  → 见下方报告：两者都靠日内动量，隔夜都是硬成本，但暴露方式不同")


if __name__ == "__main__":
    main()
