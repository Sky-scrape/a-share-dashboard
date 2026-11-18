# -*- coding: utf-8 -*-
"""C20：主力资金流 → 次日收益（P0 缺口验证）。

口径（沿用 HANDOFF 判据）：
- 主口径 r_oc：T 日收盘买 → T+1 收盘卖，扣双边费 0.15%
- r_oo：T 日收盘买 → T+1 开盘卖；overnight/intraday 拆解收益来源
- 可交易：tradable（T 日未封板 且 T+1 非一字板）
- 分档阈值一律取训练段分位；训练/验证按日期前后切半（纯时间切分，非随机）
- 显著性用日级聚合 t（按日求均值后检验），避免同日相关

数据限制：本次只取到「主力资金流向」的逐日序列；
特大单/中单/小单只返回了区间汇总列（无逐日），当日额度用尽，留作缺口。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
OUT.mkdir(parents=True, exist_ok=True)

FIELDS = {"主力资金流向": "mf", "特大单净买入额": "xl", "中单净买入额": "md", "小单净买入额": "sm"}


def load_mf():
    frames = []
    for f in sorted((BASE / "raw" / "moneyflow").glob("mf_*_*.json")):
        if ".part" in f.name:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        rows = d["datas"]
        if not rows:
            continue
        df = pd.DataFrame(rows).drop_duplicates(subset="股票代码", keep="last")
        colmap = {}
        for col in df.columns:
            if "[" not in col:
                continue
            head, tail = col.split("[", 1)
            day = tail.rstrip("]")
            if len(day) != 8 or not day.isdigit():
                continue
            for cn, en in FIELDS.items():
                if head == cn:
                    colmap[col] = f"{en}|{day[:4]}-{day[4:6]}-{day[6:]}"
        if not colmap:
            continue
        sub = df[["股票代码"] + list(colmap.keys())].copy()
        sub.columns = ["thscode"] + list(colmap.values())
        frames.append(sub)
        days = sorted({v.split("|")[1] for v in colmap.values()})
        print(f"  {f.name}: {len(sub)} 票, {len(days)} 交易日 {days[0]}~{days[-1]}")
    if not frames:
        raise SystemExit("无可用资金流文件")
    wide = pd.concat(frames, ignore_index=True)
    long = wide.melt(id_vars="thscode", var_name="k", value_name="value").dropna(subset=["value"])
    long[["field_en", "date"]] = long["k"].str.split("|", expand=True)
    out = (long.pivot_table(index=["thscode", "date"], columns="field_en",
                            values="value", aggfunc="last").reset_index())
    out["date"] = pd.to_datetime(out["date"])
    out["thscode"] = out["thscode"].astype(str)
    return out


def daily_t(df, col):
    g = df.groupby("date")[col].mean().dropna()
    if len(g) < 6:
        return np.nan, len(g)
    return g.mean() / (g.std(ddof=1) / np.sqrt(len(g))), len(g)


def stats(df, label):
    if len(df) == 0:
        return None
    t, nd = daily_t(df, "r_oc")
    return {"桶": label, "n": len(df), "净收益%": round(df["r_oc"].mean(), 3),
            "日级t": round(t, 2) if t == t else None, "天数": nd,
            "胜率%": round((df["r_oc"] > 0).mean() * 100, 1),
            "隔夜%": round(df["overnight"].mean(), 3),
            "日内%": round(df["intraday"].mean(), 3)}


def show(x):
    if x:
        print(f"  {x['桶']:<22s} n={x['n']:>6d} {x['净收益%']:+7.3f}% "
              f"t={str(x['日级t']):>5s} 胜率{x['胜率%']:5.1f}% "
              f"隔夜{x['隔夜%']:+6.2f} 日内{x['日内%']:+6.2f}")


def main():
    print("[1] 载入资金流")
    mf = load_mf()
    print(f"  合计 {len(mf)} 行, {mf['date'].nunique()} 个交易日 {mf['date'].min().date()}~{mf['date'].max().date()}")
    print(f"  字段 {list(mf.columns)}")

    print("[2] 并入主面板")
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    df = p.merge(mf, on=["thscode", "date"], how="inner")
    cov = df.groupby("date").size()
    print(f"  匹配 {len(df)} 行 / {df['date'].nunique()} 天，每日覆盖中位 {cov.median():.0f} 只")

    df = df[df["tradable"] == 1].copy()
    df = df[df["r_oc"].notna() & df["mf"].notna()].copy()
    df["mf_r"] = df["mf"] / df["amount"] * 100          # 主力净额占成交额 %

    dates = np.sort(df["date"].unique())
    cut = dates[len(dates) // 2]
    df["seg"] = np.where(df["date"] < cut, "train", "test")
    df.to_parquet(OUT / "panel_mf.parquet", index=False)
    print(f"  已落盘 panel_mf.parquet（{len(df)} 行）")
    dtr, dte = df[df.seg == "train"], df[df.seg == "test"]
    print(f"\n[3] 可交易子集：训练 {dtr['date'].nunique()} 天 ({str(pd.Timestamp(dates[0]).date())}~{str(pd.Timestamp(cut).date())}) "
          f"n={len(dtr)} {dtr['r_oc'].mean():+.3f}% | "
          f"验证 {dte['date'].nunique()} 天 n={len(dte)} {dte['r_oc'].mean():+.3f}%")

    rows = []

    def push(a, b=None):
        for x in (a, b):
            if x:
                rows.append(x)

    print("\n[4] 主力净额/成交额 五档（阈值取训练段分位）")
    qs = np.unique(dtr["mf_r"].quantile(np.linspace(0, 1, 6)).values)
    lab = pd.cut(df["mf_r"], bins=qs, labels=False, include_lowest=True)
    for b in sorted(pd.unique(lab.dropna())):
        s = df[lab == b]
        a1, a2 = stats(s[s.seg == "train"], f"主力净额占比 Q{int(b)+1} 训"), stats(s[s.seg == "test"], f"主力净额占比 Q{int(b)+1} 验")
        push(a1, a2)
        show(a1)
        show(a2)

    print("\n[5] 方向二分")
    for side, sel in [("净流入", df["mf"] > 0), ("净流出", df["mf"] < 0)]:
        s = df[sel]
        a1, a2 = stats(s[s.seg == "train"], f"主力{side} 训"), stats(s[s.seg == "test"], f"主力{side} 验")
        push(a1, a2)
        show(a1)
        show(a2)

    print("\n[6] 小市值(<30亿) 交叉")
    small = df[df["cap"] < 30]
    push(*[stats(small[small.seg == g], f"小市值 {lb}") for g, lb in [("train", "训"), ("test", "验")]])
    show(stats(small[small.seg == "train"], "小市值 训"))
    show(stats(small[small.seg == "test"], "小市值 验"))
    for side, sel in [("净流入", small["mf"] > 0), ("净流出", small["mf"] < 0)]:
        s = small[sel]
        a1, a2 = stats(s[s.seg == "train"], f"小市值+主力{side} 训"), stats(s[s.seg == "test"], f"小市值+主力{side} 验")
        push(a1, a2)
        show(a1)
        show(a2)

    print("\n[7] 主力净额占比 前 10% vs 后 10%（极端组，训练定阈）")
    hi, lo = dtr["mf_r"].quantile(0.9), dtr["mf_r"].quantile(0.1)
    for nm, sel in [("主力大幅净流入 Top10%", df["mf_r"] >= hi), ("主力大幅净流出 Bot10%", df["mf_r"] <= lo)]:
        s = df[sel]
        a1, a2 = stats(s[s.seg == "train"], f"{nm} 训"), stats(s[s.seg == "test"], f"{nm} 验")
        push(a1, a2)
        show(a1)
        show(a2)

    print("\n[8] 主力净额 × 换手率 交叉（低换手为历史正向区）")
    tq = dtr["turn"].median()
    for nm, sel in [("主力流入+低换手", (df["mf"] > 0) & (df["turn"] <= tq)),
                    ("主力流出+低换手", (df["mf"] < 0) & (df["turn"] <= tq)),
                    ("主力流入+高换手", (df["mf"] > 0) & (df["turn"] > tq)),
                    ("主力流出+高换手", (df["mf"] < 0) & (df["turn"] > tq))]:
        s = df[sel]
        a1, a2 = stats(s[s.seg == "train"], f"{nm} 训"), stats(s[s.seg == "test"], f"{nm} 验")
        push(a1, a2)
        show(a1)
        show(a2)

    pd.DataFrame([r for r in rows if r]).to_csv(OUT / "mf_scan.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT}")


if __name__ == "__main__":
    main()
