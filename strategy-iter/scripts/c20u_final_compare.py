# -*- coding: utf-8 -*-
"""C20u：剔除前视条件后的严格对比（c20t 的第二处纠错）。

c20t 的问题
-----------
我给新池也套了 C7 的完整买点条件，其中包括「收盘高于开盘」。
这个条件用的是 **T+1 收盘** 的信息，却用来决定 **T+1 开盘** 是否买入 —— 前视偏差。
实测：加这条之前笔均毛 +0.338%，加之后 +1.655%，一条规则凭空造出 1.3pp。
（与 C16 第 17 节「T+1 开盘 0~5% 筛选造成 +0.335% 假象」是同一类错误，第二次犯。）

C7 自身为什么没这个问题
------------------------
C7 是打板策略，它的「收盘高于开盘」在语料里是**风控离场条件**（收盘价低于开盘就放弃），
不是买入筛选条件；而且 C15 的官方口径已经在 T+2 制度下重算过，是可信的。

本脚本用**无前视**口径重算新池：
  T 日收盘选出（当天就能知道的信息）→ T+1 开盘买 → T+1 收盘卖
这是新池原本的定义（r_oc），也与实盘可执行的时点一致。
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
COST = 0.15


def load():
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    cols = ["date", "thscode", "tradable", "r_oc", "r_oo", "overnight",
            "intraday", "cap", "turn", "lower", "chg", "open", "close"]
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    out = {}
    for c in [x for x in cols if x in names]:
        col = pf.read(columns=[c]).column(0).combine_chunks()
        if c == "date":
            ts = pc.cast(col, "timestamp[s]")
            out[c] = pd.to_datetime(np.asarray(ts.cast("int64")) * 10**9)
        elif c == "tradable":
            out[c] = np.asarray(col.cast("int8"))
        elif c == "thscode":
            out[c] = np.asarray(col.to_pylist())
        else:
            out[c] = np.asarray(pc.cast(col, "float32"))
    d = pd.DataFrame(out, copy=False)
    d["date"] = pd.to_datetime(d["date"])
    return d


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")
    pool = (df["cap"] < 100) & (df["turn_pct"] <= 0.5) & (df["lower_p"] <= 0.3)

    a, b = pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-30")
    seg = (df["date"] >= a) & (df["date"] <= b)

    print("[1] 新池无前视口径（T 收盘选 → T+1 收盘卖，即 r_oc）")
    s = df[pool & seg]
    print(f"  n={len(s)} 笔均毛 {s['r_oc'].mean():+.3f}% 笔均净 "
          f"{s['r_oc'].mean()-COST:+.3f}%")
    gd = (s["r_oc"] - COST).groupby(s["date"]).mean()
    t = gd.mean() / (gd.std(ddof=1) / np.sqrt(len(gd)))
    print(f"  日级 t={t:.2f}（{len(gd)} 日）")

    print("\n[2] 每日全池等权买入的日均收益（这才是可执行的资金曲线）")
    daily = df[pool].groupby("date")["r_oc"].mean()
    for nm, m in [("07-08", (daily.index >= pd.Timestamp("2026-07-01"))
                   & (daily.index <= pd.Timestamp("2026-08-31"))),
                  ("09-10", (daily.index >= pd.Timestamp("2026-09-01"))
                   & (daily.index <= pd.Timestamp("2026-10-30"))),
                  ("07-10", (daily.index >= a) & (daily.index <= b))]:
        d = daily[m]
        nav = float((1 + (d - COST) / 100).prod())
        print(f"  {nm}: {len(d)}日 日均毛{d.mean():+.3f}% 扣费日{d.mean()-COST:+.3f}% "
              f"净值{nav:.4f}")

    print("\n[3] 最终对照表（同费率 0.15%、同持仓 1 日、无前视）")
    print(f"  {'策略':<20s}{'07-08':>10s}{'09-10':>10s}{'07-10':>10s}{'日均笔数':>10s}")
    c7 = (0.81, -1.17, -0.12)
    new = []
    for lo, hi in [("2026-07-01", "2026-08-31"), ("2026-09-01", "2026-10-30"),
                   ("2026-07-01", "2026-10-30")]:
        d = daily[(daily.index >= pd.Timestamp(lo)) & (daily.index <= pd.Timestamp(hi))]
        new.append(d.mean() - COST)
    cnt = len(df[pool & seg]) / df[pool & seg]["date"].nunique()
    print(f"  {'C7（涨停打板）':<20s}" + "".join(f"{x:>10.2f}" for x in c7) + f"{1.06:>10.2f}")
    print(f"  {'新池（三因子）':<20s}" + "".join(f"{x:>10.3f}" for x in new)
          + f"{cnt:>10.2f}")

    print("\n[4] 收益来源（两者都过夜，这是共同硬成本）")
    for nm, m in [("新池 07-08", pool & (df["date"] >= pd.Timestamp("2026-07-01"))
                   & (df["date"] <= pd.Timestamp("2026-08-31"))),
                  ("新池 09-10", pool & (df["date"] >= pd.Timestamp("2026-09-01"))
                   & (df["date"] <= b))]:
        x = df[m]
        print(f"  {nm}: 总{x['r_oc'].mean():+.3f}% = 隔夜{x['overnight'].mean():+.3f}% "
              f"+ 日内{x['intraday'].mean():+.3f}%")
    print("  C7 07-10（C15 复核）：隔夜 −0.96%~−1.09% / 日内 +0.05%~+2.05%")

    print("\n[5] 把 c20t 的错误量化（留痕）")
    print("  加「收盘>开盘」条件前后：笔均毛 +0.338% → +1.655%（虚增 1.32pp）")
    print("  该条件用 T+1 收盘信息决定 T+1 开盘是否买入 → 典型前视偏差")
    print("  C16 第 17 节已因同类错误纠正过一次，这是第二次，已加入检查清单")

    pd.DataFrame([{"策略": "C7", "07-08": c7[0], "09-10": c7[1], "07-10": c7[2],
                   "日均笔数": 1.06},
                  {"策略": "新池三因子", "07-08": round(new[0], 3),
                   "09-10": round(new[1], 3), "07-10": round(new[2], 3),
                   "日均笔数": round(cnt, 2)}]).to_csv(
        OUT / "c20u_final_compare.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20u_final_compare.csv'}")


if __name__ == "__main__":
    main()
