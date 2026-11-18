# -*- coding: utf-8 -*-
"""C20l：在基准池「cap<100亿 + 换手分位≤50%」内，系统扫描可叠加的因子。

为什么这里特别容易自欺
----------------------
基准池 OOS 只有 38 个交易日。如果无脑测 60 个因子，按 t 值挑最高的几个，
必然能挑出「显著」的——那是多重比较，不是发现。所以先立筛选规则，再看结果：

四判据（全部满足才算候选）：
  R1  训练段超额 > +0.10% 且 t > 1.5
  R2  样本外超额 > +0.05% 且 t > 1.96
  R3  逐月：7/8/9/10 四个月中至少 3 个月超额为正
  R4  增量：相对基准池的超额提升 > +0.03pp

附加否决条件：
  D1  与基准池已用的换手分位相关 |ρ| > 0.6  → 视为同义，不重复加
  D2  加入后池子日均只数 < 80       → 太小，噪声不可控
  D3  单月剔除后 t 掉到 1.5 以下     → 结论不稳

输出：先给全部因子的原始扫描（含未通过的），再做幸存者的深度检验。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
TRAIN_END = pd.Timestamp("2026-08-31")
COST = 0.15

COLS = ["thscode", "date", "tradable", "r_oc", "cap", "turn", "amount",
        "volr", "vol_trend", "amt_rank60", "turn_ma5", "turn_ma20",
        "turn_cum60", "turn_std60", "turn_spike", "turn_trend",
        "pos60", "to_hi60", "new_hi60", "dev20", "dev60", "ret20", "ret60",
        "atr_pct", "amp", "upper", "lower", "body", "gap",
        "profit20", "profit60", "above_ratio", "cost_disp",
        "ma_bull", "vol_price_div", "vol_up", "shrink_pull",
        "rel_str", "chg", "close", "vwap20", "vwap60", "vol_ma20",
        "dt_net_rate", "dt_org_net", "dt_on", "dt_net_pos",
        "hot_rank", "hot_on", "hi60", "lo60", "ma20", "mkt_chg"]

FACTORS = {
    "量能": ["volr", "vol_trend", "amt_rank60", "turn_ma5", "turn_ma20",
             "turn_cum60", "turn_std60", "turn_spike", "turn_trend"],
    "价格位置": ["pos60", "to_hi60", "dev20", "dev60", "ret20", "ret60"],
    "波动振幅": ["atr_pct", "amp", "upper", "lower", "body", "gap"],
    "筹码获利": ["profit20", "profit60", "above_ratio", "cost_disp"],
    "形态量价": ["ma_bull", "vol_price_div", "vol_up", "shrink_pull"],
    "相对强度": ["rel_str"],
    "龙虎榜": ["dt_net_rate", "dt_org_net", "dt_on", "dt_net_pos"],
    "人气": ["hot_rank", "hot_on"],
}


def daily_t(s, col="r_oc_x"):
    g = s.groupby("date")[col].mean().dropna()
    if len(g) < 4:
        return np.nan, len(g)
    return g.mean() / (g.std(ddof=1) / np.sqrt(len(g))), len(g)


def summarize(s):
    """返回 (超额%, t, 天数, 日均只数, 绝对%, 净值, 月超额dict)"""
    if len(s) < 80:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    t = gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))
    gm = s.groupby("date")["r_oc"].mean()
    mon = s.assign(m=s["date"].dt.month).groupby("m")["r_oc_x"].mean()
    return {"n": len(s), "日均": len(s) / len(gm), "超额": float(s["r_oc_x"].mean()),
            "t": float(t), "绝对": float(s["r_oc"].mean()),
            "净值": float((1 + (gm - COST) / 100).prod()),
            "月": {int(k): float(v) for k, v in mon.items()}}


def main():
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet",
                        columns=COLS)
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    for c in COLS:
        if c not in ("thscode", "date") and p[c].dtype == "float64":
            p[c] = p[c].astype("float32")
    df = p[p["tradable"] == 1].reset_index(drop=True)
    del p
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    g = df.groupby("date")
    df["turn_pct"] = g["turn"].rank(pct=True).astype("float32")
    df["cap_pct"] = g["cap"].rank(pct=True).astype("float32")

    base = (df["cap"] < 100) & (df["turn_pct"] <= 0.5)
    tr, oos = df["date"] <= TRAIN_END, df["date"] > TRAIN_END
    b_tr = summarize(df[base & tr])
    b_oos = summarize(df[base & oos])
    print("[0] 基准池：cap<100亿 + 换手分位≤50%")
    print(f"  训练: n={b_tr['n']:>6d}(日均{b_tr['日均']:>4.0f}) 超额{b_tr['超额']:+7.3f}% "
          f"t={b_tr['t']:>5.2f} 净值{b_tr['净值']:.4f}")
    print(f"  OOS : n={b_oos['n']:>6d}(日均{b_oos['日均']:>4.0f}) 超额{b_oos['超额']:+7.3f}% "
          f"t={b_oos['t']:>5.2f} 净值{b_oos['净值']:.4f} 月超额 "
          f"{ {k: round(v*100, 3) for k, v in b_oos['月'].items()} }")
    print(f"  → 新因子必须在 OOS 超额 {b_oos['超额']:+.3f}% / t={b_oos['t']:.2f} 之上有增量")
    print(f"  → 门槛 R1: 训练超额>{b_tr['超额']+0.10:+.3f}% R2: OOS超额>{b_oos['超额']+0.05:+.3f}%")

    base_tr_ex, base_oos_ex = b_tr["超额"], b_oos["超额"]
    base_oos_t = b_oos["t"]

    # 池内每日分位
    for c in {c for v in FACTORS.values() for c in v}:
        if c in ("ma_bull", "vol_price_div", "vol_up", "shrink_pull",
                 "dt_on", "hot_on", "new_hi60"):
            continue
        df[c + "_p"] = df.groupby("date")[c].rank(pct=True)

    print("\n[1] 原始扫描：池内 Q1(最低20%) 与 Q5(最高20%)")
    print(f"  {'因子':<14s}{'端':<4s}{'训练超额':>10s}{'t':>6s}"
          f"{'OOS超额':>10s}{'t':>6s}{'日均':>6s}{'月正数':>7s}")
    raw = []
    for grp, cols in FACTORS.items():
        for c in cols:
            pcol = c + "_p"
            if pcol not in df.columns:
                # 二元因子：直接用 0/1
                for val, end in [(1, "=1")]:
                    m = base & (df[c] == val)
                    a = summarize(df[m & tr])
                    b = summarize(df[m & oos])
                    if not a or not b:
                        continue
                    mp = sum(1 for v in b["月"].values() if v > 0)
                    raw.append({"组": grp, "因子": c, "端": end, "训超额": a["超额"],
                                "训t": a["t"], "OOS超额": b["超额"], "OOS_t": b["t"],
                                "日均": b["日均"], "月正数": mp,
                                "训净值": a["净值"], "OOS净值": b["净值"]})
                    print(f"  {c:<14s}{end:<4s}{a['超额']:>9.3f}%{a['t']:>6.2f}"
                          f"{b['超额']:>9.3f}%{b['t']:>6.2f}{b['日均']:>6.0f}{mp:>5d}/4")
                continue
            for tail, end in [(0.2, "低"), (0.8, "高")]:
                m = base & (df[pcol] <= tail if tail < 0.5 else df[pcol] >= tail)
                a = summarize(df[m & tr])
                b = summarize(df[m & oos])
                if not a or not b:
                    continue
                mp = sum(1 for v in b["月"].values() if v > 0)
                raw.append({"组": grp, "因子": c, "端": end, "训超额": a["超额"],
                            "训t": a["t"], "OOS超额": b["超额"], "OOS_t": b["t"],
                            "日均": b["日均"], "月正数": mp,
                            "训净值": a["净值"], "OOS净值": b["净值"]})
                print(f"  {c:<14s}{end:<4s}{a['超额']:>9.3f}%{a['t']:>6.2f}"
                      f"{b['超额']:>9.3f}%{b['t']:>6.2f}{b['日均']:>6.0f}{mp:>5d}/4")

    r = pd.DataFrame(raw)
    r.to_csv(OUT / "c20l_raw_scan.csv", index=False, encoding="utf-8-sig")

    print("\n[2] 四判据筛选")
    r["R1"] = (r["训超额"] > base_tr_ex + 0.10) & (r["训t"] > 1.5)
    r["R2"] = (r["OOS超额"] > base_oos_ex + 0.05) & (r["OOS_t"] > 1.96)
    r["R3"] = r["月正数"] >= 3
    r["R4"] = r["OOS超额"] > base_oos_ex + 0.03
    r["D2"] = r["日均"] >= 80
    r["通过"] = r["R1"] & r["R2"] & r["R3"] & r["R4"] & r["D2"]
    surv = r[r["通过"]].sort_values("OOS_t", ascending=False)
    print(f"  共扫描 {len(r)} 个（因子×端），通过 {len(surv)} 个")
    if len(surv):
        print(surv[["组", "因子", "端", "训超额", "训t", "OOS超额", "OOS_t", "日均", "月正数"]]
              .to_string(index=False))
    else:
        print("  （无）")

    print("\n[3] 次优观察池：仅差 R1（训练段不够强）但 OOS 强的")
    near = r[(r["R2"]) & (r["R3"]) & (r["D2"]) & (~r["R1"])].sort_values("OOS_t",
                                                                         ascending=False)
    if len(near):
        print(near[["组", "因子", "端", "训超额", "训t", "OOS超额", "OOS_t", "日均"]]
              .head(12).to_string(index=False))
        print("  提示：训练段不够强 = 可能只是 9-10 月的风格，不能当因子用")
    else:
        print("  （无）")

    r.to_csv(OUT / "c20l_screen.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20l_raw_scan.csv'} / c20l_screen.csv")


if __name__ == "__main__":
    main()
