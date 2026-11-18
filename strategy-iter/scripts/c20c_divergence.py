# -*- coding: utf-8 -*-
"""C20c：小单/特大单逐日补齐后，①真样本外检验「小市值+低换手」是否在 9-10 月仍成立；
②检验「小单与主力背离」。

数据实况（本轮取数结果，必须写进口径）：
- 特大单净买入额 / 小单净买入额 逐日：7、8、9 三个月完整（23/21/21 个交易日）。
  10 月只取到 500 只（000001~001391，按代码升序），非随机子集 → 10 月不纳入资金流检验。
- 主力资金流向 逐日：仅 7、8 月（9-10 月因当日额度耗尽未取到）。
  因此「小单 vs 主力」背离只用 7-8 月；「小单 vs 特大单」背离可用 7-9 月。

样本外切分（与 C20 不同，这次是真 OOS）：
- 训练池 = 2026-07-01 ~ 2026-08-31（C20 当时的全部数据）
- 样本外 = 2026-09-01 ~ 2026-10-30
- 阈值一律用训练池分位，样本外不重算。

收益口径沿用 engine：r_oc（T 收盘买 → T+1 收盘卖）、tradable 掩码。
显著性用日级 t（先按日求桶均值，再对日序列检验），桶内超额=当日减全市场均值。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
OUT.mkdir(parents=True, exist_ok=True)

TRAIN_END = pd.Timestamp("2026-08-31")
MONTHS = {"2026-07": "7月", "2026-08": "8月", "2026-09": "9月", "2026-10": "10月"}


def load_wide(path, fields):
    """宽表 json -> DataFrame[thscode, field, date, v] 长表。"""
    df = pd.DataFrame(json.loads(path.read_text(encoding="utf-8"))["datas"])
    codes = df["股票代码"].astype(str).values
    recs = []
    for c in df.columns:
        if "[" not in c:
            continue
        head, tail = c.split("[", 1)
        day = tail.rstrip("]")
        if len(day) != 8 or not day.isdigit() or head not in fields:
            continue
        vals = pd.to_numeric(df[c], errors="coerce").values
        keep = ~np.isnan(vals)
        if not keep.any():
            continue
        recs.append(pd.DataFrame({"thscode": codes[keep], "field": head,
                                  "date": pd.Timestamp(day), "v": vals[keep]}))
    if not recs:
        return None
    return pd.concat(recs, ignore_index=True)


def melt_flow(files, fields, label):
    frames = []
    for f in files:
        long = load_wide(f, fields)
        if long is None:
            continue
        pv = long.pivot_table(index=["thscode", "date"], columns="field", values="v",
                              aggfunc="last").reset_index()
        frames.append(pv)
        print(f"  {label} {f.name}: {pv['thscode'].nunique()} 票 / "
              f"{pv['date'].nunique()} 日", flush=True)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["thscode", "date"], keep="last")


def day_t(s, col="r_oc_x"):
    g = s.groupby("date")[col].mean().dropna()
    if len(g) < 4:
        return np.nan, len(g)
    return g.mean() / (g.std(ddof=1) / np.sqrt(len(g))), len(g)


def stat(df, mask, label):
    s = df[mask]
    s = s[s["r_oc_x"].notna()]
    if len(s) < 100:
        return None
    t, nd = day_t(s)
    return {"桶": label, "n": len(s), "超额%": round(float(s["r_oc_x"].mean()), 3),
            "日级t": round(float(t), 2) if t == t else None, "天数": nd,
            "绝对收益%": round(float(s["r_oc"].mean()), 3),
            "胜率%": round(float((s["r_oc"] > 0).mean() * 100), 1)}


def show(x):
    if x:
        print(f"  {x['桶']:<34s} n={x['n']:>6d} 超额{x['超额%']:+7.3f}% "
              f"t={str(x['日级t']):>6s}({x['天数']:>2d}日) 绝对{x['绝对收益%']:+7.3f}% "
              f"胜率{x['胜率%']:5.1f}%")


def prep(df):
    df["r_oc_x"] = df["r_oc"] - df.groupby("date")["r_oc"].transform("mean")
    df["mon"] = df["date"].dt.to_period("M").astype(str)
    return df


def main():
    rows = []
    print("[1] 载入逐日资金流")

    f2_files = sorted((BASE / "raw" / "moneyflow2").glob("flow2_*.json"))
    f2_files = [f for f in f2_files if ".part" not in f.name]
    f2 = melt_flow(f2_files, {"特大单净买入额", "小单净买入额"}, "flow2")

    mf_files = sorted((BASE / "raw" / "moneyflow").glob("mf_*.json"))
    mf = melt_flow(mf_files, {"主力资金流向"}, "mf")

    print("\n[2] 并入主面板")
    p = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    p["date"] = pd.to_datetime(p["date"])
    p["thscode"] = p["thscode"].astype(str)
    p = p[p["tradable"] == 1].copy()
    base = prep(p.copy())

    # ---------- A. 真样本外：小市值+低换手 ----------
    print("\n[A] 真样本外：小市值 + 低换手（阈值取 7-8 月分位）")
    print("    为什么这是本轮最重要的检验：C20 的 +0.42%/+0.33% 是 7-8 月内部切半，"
          "现在 9-10 月是没见过的新数据。")
    dtr = base[base["date"] <= TRAIN_END]
    turn_q = float(dtr["turn"].median())
    print(f"    训练池低换手阈值（turn 中位，7-8月）= {turn_q:.2f}%")

    combos = [
        ("全市场基线", pd.Series(True, index=base.index)),
        ("小市值<30亿", base["cap"] < 30),
        ("低换手", base["turn"] <= turn_q),
        ("小市值+低换手", (base["cap"] < 30) & (base["turn"] <= turn_q)),
        ("小市值<20亿+低换手", (base["cap"] < 20) & (base["turn"] <= turn_q)),
        ("小市值+低换手+低波动", (base["cap"] < 30) & (base["turn"] <= turn_q)
         & (base["atr_pct"] <= float(dtr["atr_pct"].median()))),
    ]
    segs = [("训练 7-8月", base["date"] <= TRAIN_END),
            ("样本外 9月", base["mon"] == "2026-09"),
            ("样本外 10月", base["mon"] == "2026-10"),
            ("样本外 9-10月合计", base["date"] > TRAIN_END)]
    for nm, m in combos:
        for sl, sm_ in segs:
            x = stat(base, m & sm_, f"{nm} | {sl}")
            if x:
                rows.append(x)
                show(x)
        print()

    # ---------- B. 小单 vs 特大单 背离（7-9月） ----------
    print("[B] 小单 vs 特大单 背离（7-9 月，逐日字段齐）")
    if f2 is not None:
        d = base.merge(f2.rename(columns={"特大单净买入额": "xl", "小单净买入额": "sm"}),
                       on=["thscode", "date"], how="inner")
        d["xl_r"] = d["xl"] / d["amount"] * 100
        d["sm_r"] = d["sm"] / d["amount"] * 100
        d = prep(d)
        print(f"    覆盖 {len(d)} 行 / {d['date'].nunique()} 天，每日中位 "
              f"{d.groupby('date').size().median():.0f} 只")
        dtr2 = d[d["date"] <= TRAIN_END]
        print(f"    逐日 rank 相关（全样本）：corr(xl_r, r_oc) 中位 "
              f"{_rank_corr(d, 'xl_r'):+.3f}，corr(sm_r, r_oc) 中位 {_rank_corr(d, 'sm_r'):+.3f}")

        # 方向二分
        for nm, m in [("特大单净流入", d["xl"] > 0), ("特大单净流出", d["xl"] < 0),
                      ("小单净流入(散户接盘)", d["sm"] > 0), ("小单净流出(散户离场)", d["sm"] < 0)]:
            for sl, sm_ in [("训练 7-8月", d["date"] <= TRAIN_END),
                            ("样本外 9月", d["mon"] == "2026-09")]:
                x = stat(d, m & sm_, f"{nm} | {sl}")
                if x:
                    rows.append(x)
                    show(x)
        print()

        print("    背离定义：特大单净流入 且 小单净流出（主力买 / 散户卖）")
        for nm, m in [("背离：主力买+散户卖", (d["xl"] > 0) & (d["sm"] < 0)),
                      ("共识：主力买+散户买", (d["xl"] > 0) & (d["sm"] > 0)),
                      ("背离：主力卖+散户买", (d["xl"] < 0) & (d["sm"] > 0)),
                      ("共识：主力卖+散户卖", (d["xl"] < 0) & (d["sm"] < 0))]:
            for sl, sm_ in [("训练 7-8月", d["date"] <= TRAIN_END),
                            ("样本外 9月", d["mon"] == "2026-09")]:
                x = stat(d, m & sm_, f"{nm} | {sl}")
                if x:
                    rows.append(x)
                    show(x)
        print()

        # 连续强度：xl_r - sm_r
        diff = d["xl_r"] - d["sm_r"]
        qs = np.unique(dtr2["xl_r"].sub(dtr2["sm_r"]).quantile(np.linspace(0, 1, 6)).values)
        if len(qs) > 2:
            lab = pd.cut(diff, bins=qs, labels=False, include_lowest=True)
            for b in sorted(pd.unique(lab.dropna())):
                for sl, sm_ in [("训练 7-8月", d["date"] <= TRAIN_END),
                                ("样本外 9月", d["mon"] == "2026-09")]:
                    x = stat(d, (lab == b) & sm_, f"特大-小单强度 Q{int(b)+1} | {sl}")
                    if x:
                        rows.append(x)
                        show(x)

        # 有效池「小市值+低换手」里加背离
        print("\n    在有效池「小市值+低换手」里加背离（检验增量）")
        pool = (d["cap"] < 30) & (d["turn"] <= turn_q)
        for nm, m in [("池内全", pd.Series(True, index=d.index)),
                      ("池内+主力买散户卖", (d["xl"] > 0) & (d["sm"] < 0)),
                      ("池内+主力卖散户买", (d["xl"] < 0) & (d["sm"] > 0))]:
            for sl, sm_ in [("训练 7-8月", d["date"] <= TRAIN_END),
                            ("样本外 9月", d["mon"] == "2026-09")]:
                x = stat(d, pool & m & sm_, f"{nm} | {sl}")
                if x:
                    rows.append(x)
                    show(x)

    # ---------- C. 小单 vs 主力（7-8 月） ----------
    print("\n[C] 小单 vs 主力 背离（仅 7-8 月，9-10 月主力逐日未取到）")
    if mf is not None and f2 is not None:
        dm = base.merge(mf.rename(columns={"主力资金流向": "mf"}), on=["thscode", "date"], how="inner")
        dm = dm.merge(f2.rename(columns={"特大单净买入额": "xl", "小单净买入额": "sm"}),
                      on=["thscode", "date"], how="inner")
        dm = prep(dm)
        dm["mf_r"] = dm["mf"] / dm["amount"] * 100
        dm["sm_r"] = dm["sm"] / dm["amount"] * 100
        print(f"    覆盖 {len(dm)} 行 / {dm['date'].nunique()} 天；"
              f"corr(mf_r, sm_r) 逐日中位 {_rank_corr(dm, 'sm_r', 'mf_r'):+.3f}")
        for nm, m in [("主力买+散户卖(经典背离)", (dm["mf"] > 0) & (dm["sm"] < 0)),
                      ("主力买+散户买", (dm["mf"] > 0) & (dm["sm"] > 0)),
                      ("主力卖+散户买", (dm["mf"] < 0) & (dm["sm"] > 0)),
                      ("主力卖+散户卖", (dm["mf"] < 0) & (dm["sm"] < 0))]:
            x = stat(dm, m, f"{nm} | 7-8月")
            if x:
                rows.append(x)
                show(x)
        # 训练内部切半再确认（7月 vs 8月）
        for nm, m in [("主力买+散户卖", (dm["mf"] > 0) & (dm["sm"] < 0))]:
            for sl, sm_ in [("7月", dm["mon"] == "2026-07"), ("8月", dm["mon"] == "2026-08")]:
                x = stat(dm, m & sm_, f"{nm} | {sl}")
                if x:
                    rows.append(x)
                    show(x)

    res = pd.DataFrame([r for r in rows if r])
    res.to_csv(OUT / "c20c_divergence.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20c_divergence.csv'} ({len(res)} 桶)")


def _rank_corr(df, col, y="r_oc"):
    cs = []
    for _, g in df.groupby("date"):
        a, b = g[col].rank(), g[y].rank()
        if len(g) > 30 and a.std() > 0 and b.std() > 0:
            cs.append(a.corr(b))
    return float(pd.Series(cs).median()) if cs else np.nan


if __name__ == "__main__":
    main()
