# -*- coding: utf-8 -*-
"""C20m：修正判据后重扫（c20l 的 R3 有 bug，OOS 只有 9/10 两个月）。

修正两点
--------
1. R3 原写「4 个月中至少 3 个月为正」，但 OOS 只有 9、10 两个月 → 不可能通过。
   改为：把 7/8/9/10 四个自然月各自单独统计，要求**至少 3 个月为正**（跨训练+样本外）。
2. 比较基准从「子集 vs 全池」改为「子集 vs 补集」。
   池内打分位后取 20%，子集超额天然低于全池均值，用全池当基准会系统性偏严。
   正确问法是：这个因子的两侧是否有差异（多空差）。

判据（全部满足）
  R1 训练段多空差 > 0.15pp（子集超额 − 补集超额）
  R2 样本外多空差 > 0.10pp 且 t(多空差) > 1.96
  R3 4 个自然月中至少 3 个月多空差为正
  R4 样本外子集超额 > 基准池 OOS 超额
  D2 子集日均只数 >= 60
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
        "hot_rank", "hot_on", "hi60", "lo60", "ma20"]

LABELS = {
    "volr": "量比", "vol_trend": "量能趋势", "amt_rank60": "成交额60日分位",
    "turn_ma5": "5日均换手", "turn_ma20": "20日均换手", "turn_cum60": "60日累计换手",
    "turn_std60": "换手波动", "turn_spike": "换手突增", "turn_trend": "换手趋势",
    "pos60": "60日位置", "to_hi60": "距60日高", "dev20": "20日偏离", "dev60": "60日偏离",
    "ret20": "20日涨幅", "ret60": "60日涨幅",
    "atr_pct": "ATR占比", "amp": "振幅", "upper": "上影", "lower": "下影",
    "body": "实体", "gap": "跳空",
    "profit20": "20日获利盘", "profit60": "60日获利盘",
    "above_ratio": "均线上方比例", "cost_disp": "筹码分散度",
    "ma_bull": "多头排列", "vol_price_div": "量价背离", "vol_up": "量增价涨",
    "shrink_pull": "缩量回调", "rel_str": "相对强度",
    "dt_net_rate": "龙虎榜净额占比", "dt_org_net": "机构净额", "dt_on": "上榜",
    "hot_rank": "人气排名", "hot_on": "人气命中",
}

GROUPS = {
    "量能": ["volr", "vol_trend", "amt_rank60", "turn_ma5", "turn_ma20",
             "turn_cum60", "turn_std60", "turn_spike", "turn_trend"],
    "价格位置": ["pos60", "to_hi60", "dev20", "dev60", "ret20", "ret60"],
    "波动振幅": ["atr_pct", "amp", "upper", "lower", "body", "gap"],
    "筹码获利": ["profit20", "profit60", "above_ratio", "cost_disp"],
    "形态量价": ["ma_bull", "vol_price_div", "vol_up", "shrink_pull"],
    "相对强度": ["rel_str"],
    "龙虎榜": ["dt_net_rate", "dt_org_net", "dt_on"],
    "人气": ["hot_rank", "hot_on"],
}


def agg(s):
    if len(s) < 60:
        return None
    gx = s.groupby("date")["r_oc_x"].mean().dropna()
    if len(gx) < 4:
        return None
    gm = s.groupby("date")["r_oc"].mean()
    return {"n": len(s), "日均": len(s) / len(gm),
            "超额": float(s["r_oc_x"].mean()),
            "t": float(gx.mean() / (gx.std(ddof=1) / np.sqrt(len(gx)))),
            "净值": float((1 + (gm - COST) / 100).prod())}


def load():
    """分块读列，逐块转 float32 / 紧凑类型，避免整表 float64 撑爆内存。"""
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    keep = [c for c in COLS if c in names]
    out = {}
    for c in keep:
        s = pf.read(columns=[c]).column(0).to_pandas()
        if c == "thscode":
            out[c] = s.astype("string[pyarrow]")
        elif c == "date":
            out[c] = pd.to_datetime(s.to_numpy())
        else:
            out[c] = pd.to_numeric(np.asarray(s), errors="coerce").astype("float32")
        del s
    return pd.DataFrame(out, copy=False)


def main():
    df = load()
    df = df[df["tradable"] == 1].reset_index(drop=True)
    df["r_oc_x"] = (df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")).astype("float32")
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["month"] = df["date"].dt.month

    base = (df["cap"] < 100) & (df["turn_pct"] <= 0.5)
    tr = df["date"] <= TRAIN_END
    oos = df["date"] > TRAIN_END
    b_tr, b_oos = agg(df[base & tr]), agg(df[base & oos])
    print("[0] 基准池 cap<100亿 + 换手分位≤50%")
    print(f"  训练 超额{b_tr['超额']:+7.3f}% t={b_tr['t']:>5.2f} 净值{b_tr['净值']:.4f} "
          f"(日均{b_tr['日均']:.0f})")
    print(f"  OOS  超额{b_oos['超额']:+7.3f}% t={b_oos['t']:>5.2f} 净值{b_oos['净值']:.4f} "
          f"(日均{b_oos['日均']:.0f})")

    for c in {x for v in GROUPS.values() for x in v}:
        if c in ("ma_bull", "vol_price_div", "vol_up", "shrink_pull", "dt_on", "hot_on"):
            continue
        df[c + "_p"] = gg[c].rank(pct=True).astype("float32")

    rows = []
    print("\n[1] 多空差扫描（池内 Q20 最低 vs Q80 最高）")
    print(f"  {'因子':<14s}{'训低%':>8s}{'训高%':>8s}{'训差pp':>8s}"
          f"{'OOS低%':>8s}{'OOS高%':>8s}{'OOS差pp':>9s}{'t差':>6s}{'月正':>5s}{'日均':>6s}")
    for grp, cols in GROUPS.items():
        for c in cols:
            pc = c + "_p"
            if pc in df.columns:
                lo_m, hi_m = base & (df[pc] <= 0.2), base & (df[pc] >= 0.8)
                lab = f"{LABELS.get(c, c)} 低-高"
            else:
                lo_m, hi_m = base & (df[c] == 1), base & (df[c] == 0)
                lab = f"{LABELS.get(c, c)} 1-0"
            L_tr, H_tr = agg(df[lo_m & tr]), agg(df[hi_m & tr])
            L_oos, H_oos = agg(df[lo_m & oos]), agg(df[hi_m & oos])
            if not all([L_tr, H_tr, L_oos, H_oos]):
                continue
            # 内部统一用「百分点」，输出时不重复乘 100
            d_tr = L_tr["超额"] - H_tr["超额"]
            d_oos = L_oos["超额"] - H_oos["超额"]
            a = df[lo_m & oos].groupby("date")["r_oc_x"].mean()
            b = df[hi_m & oos].groupby("date")["r_oc_x"].mean()
            dd = (a - b).dropna()
            t_d = float(dd.mean() / (dd.std(ddof=1) / np.sqrt(len(dd)))) if len(dd) > 3 else np.nan
            mpos = 0
            for mm in (7, 8, 9, 10):
                x = df[lo_m & (df["month"] == mm)].groupby("date")["r_oc_x"].mean()
                y = df[hi_m & (df["month"] == mm)].groupby("date")["r_oc_x"].mean()
                if len(x) and len(y) and (x.mean() - y.mean()) > 0:
                    mpos += 1
            r = {"组": grp, "因子": c, "标签": lab,
                 "训低": L_tr["超额"], "训高": H_tr["超额"], "训差": d_tr,
                 "OOS低": L_oos["超额"], "OOS高": H_oos["超额"], "OOS差": d_oos,
                 "t差": t_d, "月正": mpos,
                 "日均低": L_oos["日均"], "净值低": L_oos["净值"]}
            rows.append(r)
            print(f"  {LABELS.get(c, c):<14s}{L_tr['超额']:>8.3f}{H_tr['超额']:>8.3f}"
                  f"{d_tr:>8.3f}{L_oos['超额']:>8.3f}{H_oos['超额']:>8.3f}"
                  f"{d_oos:>9.3f}{t_d:>6.2f}{mpos:>4d}/4{L_oos['日均']:>6.0f}")

    r = pd.DataFrame(rows)
    r["R1"] = r["训差"] > 0.15
    r["R2"] = (r["OOS差"] > 0.10) & (r["t差"] > 1.96)
    r["R3"] = r["月正"] >= 3
    r["R4"] = r["OOS低"] > b_oos["超额"]
    r["D2"] = r["日均低"] >= 60
    r["通过"] = r["R1"] & r["R2"] & r["R3"] & r["R4"] & r["D2"]

    print("\n[2] 完整结果（按 OOS 多空差排序）")
    out = r.sort_values("OOS差", ascending=False)
    print(f"  {'因子':<14s}{'训差pp':>9s}{'OOS差pp':>9s}{'t差':>6s}{'月正':>5s}"
          f"{'OOS低%':>8s}{'日均':>6s}  判据")
    for _, x in out.iterrows():
        flag = "PASS" if x["通过"] else "".join(
            k for k, ok in [("R1", x["R1"]), ("R2", x["R2"]), ("R3", x["R3"]),
                            ("R4", x["R4"]), ("D2", x["D2"])] if not ok)
        print(f"  {LABELS.get(x['因子'], x['因子']):<14s}{x['训差']:>9.3f}"
              f"{x['OOS差']:>9.3f}{x['t差']:>6.2f}{int(x['月正']):>4d}/4"
              f"{x['OOS低']:>8.3f}{x['日均低']:>6.0f}  {flag}")

    surv = out[out["通过"]]
    print(f"\n[3] 通过 {len(surv)} / {len(r)}")
    if len(surv):
        for _, x in surv.iterrows():
            print(f"  ★ {x['标签']}  训差{x['训差']:+.3f}pp OOS差"
                  f"{x['OOS差']:+.3f}pp t={x['t差']:.2f} 月正{int(x['月正'])}/4 "
                  f"OOS低{x['OOS低']:+.3f}% 净值{x['净值低']:.4f} 日均{x['日均低']:.0f}")

    r.to_csv(OUT / "c20m_spread.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20m_spread.csv'}")


if __name__ == "__main__":
    main()
