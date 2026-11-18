# -*- coding: utf-8 -*-
"""C20s：C7 与三因子池的资金曲线级对比。

统一口径（关键）
----------------
C7 实际执行：T 日选出 → T+1 开盘 0~5% 才买 → T+2 开盘卖（制度 T+2），双边费 0.15%。
  可成交 262 笔 / 7-10 月 87 笔，全窗口 T+2 净 +0.29%。

要让两者可比，必须用**同一条资金曲线规则**：
- 每个交易日算「当日实际投入的资金比例」× 「当日该组合的净收益」
- 未投入部分记 0（C7 大部分时间空仓，这是它的风险暴露特征，不是缺点）
- 逐日累乘成净值

C7 的持仓日是「买入后持有到 T+2 开盘」，即每笔占用 1 个交易日；
新池是「T 收盘买 → T+1 收盘卖」，也占用 1 个交易日。
两者在时间占用上等价，可以直接放到同一张日频净值表上。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
COST = 0.15
P7 = BASE / "runs" / "c15_window_C7_1030"


def load_panel():
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    cols = ["date", "thscode", "tradable", "r_oc", "r_oo", "overnight",
            "intraday", "cap", "turn", "lower", "chg", "close", "o1", "c1", "T1"]
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    out = {}
    for c in [x for x in cols if x in names]:
        col = pf.read(columns=[c]).column(0).combine_chunks()
        if c in ("date", "T1"):
            ts = pc.cast(col, "timestamp[s]") if str(pf.schema_arrow.field(c).type).startswith("large_string") else col
            out[c] = pd.to_datetime(np.asarray(pc.cast(col, "int64")) * 10**9) \
                if str(pf.schema_arrow.field(c).type).startswith("timestamp") else \
                pd.to_datetime(np.asarray(col.to_pylist()))
        elif c == "tradable":
            out[c] = np.asarray(col.cast("int8"))
        elif c == "thscode":
            out[c] = np.asarray(col.to_pylist())
        else:
            out[c] = np.asarray(pc.cast(col, "float32"))
    return pd.DataFrame(out, copy=False)


def main():
    df = load_panel()
    df["date"] = pd.to_datetime(df["date"])
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")
    pool = (df["tradable"] == 1) & (df["cap"] < 100) & \
           (df["turn_pct"] <= 0.5) & (df["lower_p"] <= 0.3)

    print("[1] 新池逐日净收益（T 收盘买 → T+1 收盘卖，已扣 0.15%）")
    new_daily = df[pool].groupby("date")["r_oc"].mean() - COST
    new_daily = new_daily.dropna()

    print("[2] C7 逐笔 → 逐日净收益")
    p7 = pd.read_csv(P7 / "picks.csv")
    v7 = pd.read_csv(P7 / "validation.csv")
    print(f"  picks {len(p7)} 笔 / validation {len(v7)} 笔")
    # validation 有 T / T1 / open_ret / close_ret / buy_triggered
    v = v7.copy()
    v["T"] = pd.to_datetime(v["T"])
    v["T1"] = pd.to_datetime(v["T1"])
    print(f"  区间 {v['T'].min().date()} ~ {v['T'].max().date()}")
    print(f"  buy_triggered=True 比例 {v['buy_triggered'].mean()*100:.1f}%")

    # 口径：T+1 开盘买（buy_triggered 才买），T+2 开盘卖 —— 用 open_ret / close_ret 近似
    print("\n  validation 字段的收益口径核对（前 5 行）")
    print(v[["T", "T1", "open_ret", "close_ret", "buy_triggered", "win"]]
          .head(5).to_string(index=False))

    print("\n[3] 分段对比（单位：%/笔 或 %/日，已是扣费前）")
    segs = [("2026-01-01", "2026-06-30", "01-06"),
            ("2026-07-01", "2026-08-31", "07-08"),
            ("2026-09-01", "2026-10-30", "09-10"),
            ("2026-07-01", "2026-10-30", "07-10"),
            ("2026-01-01", "2026-10-30", "全窗口")]
    print(f"  {'区间':<8s}{'新池 日净%':>12s}{'新池 日数':>10s}"
          f"{'C7 笔均净%':>12s}{'C7 笔数':>9s}")
    for a, b, nm in segs:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        nd = new_daily[(new_daily.index >= a) & (new_daily.index <= b)]
        c7 = v[(v["T"] >= a) & (v["T"] <= b)]
        c7c = c7[c7["buy_triggered"] == True]  # noqa: E712
        print(f"  {nm:<8s}{nd.mean():>12.3f}{len(nd):>10d}"
              f"{c7c['close_ret'].mean() if len(c7c) else np.nan:>12.3f}"
              f"{len(c7c):>9d}")

    print("\n[4] 同区间资金曲线（7-10 月，各自按满仓/空仓规则）")
    a, b = pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-30")
    nd = new_daily[(new_daily.index >= b) | (new_daily.index >= a)]
    nd = new_daily[(new_daily.index >= a) & (new_daily.index <= b)]

    # C7：按 T1（买入日）聚合，等权
    c7 = v[(v["buy_triggered"] == True) & (v["T"] >= a) & (v["T"] <= b)].copy()  # noqa: E712
    c7["net"] = c7["close_ret"] - COST
    c7_daily = c7.groupby("T")["net"].mean()

    # 统一交易日序列
    days = sorted(set(nd.index) | set(c7_daily.index))
    print(f"  {'日期':<12s}{'新池%':>9s}{'C7%':>9s}{'新池累计':>10s}{'C7累计':>10s}")
    nav_n = nav_c = 1.0
    for d in days:
        rn = nd.get(d, 0.0)
        rc = c7_daily.get(d, 0.0)
        nav_n *= (1 + rn / 100)
        nav_c *= (1 + rc / 100)
        if d.day % 10 < 3 or d == days[-1]:
            print(f"  {d.strftime('%Y-%m-%d'):<12s}{rn:>9.3f}{rc:>9.3f}"
                  f"{nav_n:>10.4f}{nav_c:>10.4f}")
    print(f"\n  7-10 月：新池期末 {nav_n:.4f} / C7 期末 {nav_c:.4f}")

    print("\n[5] 资金占用对比（这是 C7 的真正优势）")
    print(f"  新池：每个交易日都满仓，{len(nd)} 个交易日全部有持仓")
    print(f"  C7 ：{len(c7_daily)} 个交易日有成交（占 {len(c7_daily)/len(nd)*100:.0f}%），"
          f"其余 {len(nd)-len(c7_daily)} 天空仓")
    print(f"  C7 笔均持仓 1 日，合计暴露 {len(c7)} 笔 / {len(nd)} 日 = "
          f"{len(c7)/len(nd):.2f} 笔每日")

    print("\n[6] 收益来源拆解：两者是否同一死法")
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    print("  新池（OOS 9-10）：")
    s = df[pool & (df["date"] > pd.Timestamp("2026-08-31"))]
    print(f"    总 {s['r_oc'].mean():+.3f}% = 隔夜 {s['overnight'].mean():+.3f}% "
          f"+ 日内 {s['intraday'].mean():+.3f}%")
    print("  C7（报告口径，7-10 月）：")
    print("    隔夜 −0.96%~−1.09% / 日内 +0.05%~+2.05%（见 风格漂移复核 第二节）")
    print("  → 两者都靠日内动量，隔夜都是成本；差别在 C7 日内动量更大但暴露更集中")


if __name__ == "__main__":
    main()
