"""Round-end factor analysis for one run dir.

Enriches validation rows with T-day raw factor values (pool fields for LU picks,
stock/industry indicators for NLU picks), prints bucket tables and correlations
for the round-end review. Read-only over the run dir; writes analysis.md alongside.

Usage: cd strategy-iter && python -m scripts.analyze_round runs/mboard_round1_M1
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data import build_store
from engine.precompute import precompute
from engine import rules as R


def bucket_table(v: pd.DataFrame, col: str, bins, labels) -> str:
    b = pd.cut(v[col], bins=bins, labels=labels)
    g = v.groupby(b, observed=False)["close_ret"]
    out = pd.DataFrame({
        "n": g.size(),
        "win%": (g.apply(lambda s: (s > 0).mean() if len(s) else np.nan) * 100).round(1),
        "avg%": g.mean().round(2),
    })
    out.index.name = col
    return out.to_string()


def corr_line(v: pd.DataFrame, cols) -> str:
    parts = []
    for c in cols:
        if c in v and v[c].notna().sum() > 20:
            parts.append(f"{c}:{v[c].corr(v['close_ret']):+.3f}")
    return "  ".join(parts)


def main(run_dir: str):
    run_dir = Path(run_dir)
    picks = pd.read_csv(run_dir / "picks.csv", dtype={"thscode": str})
    vals = pd.read_csv(run_dir / "validation.csv")
    v = vals[vals["close_ret"].notna()].copy()

    store = build_store()
    pre = precompute(store, R.VERSIONS["V1"])
    si = pre["si_indexed"]
    pool_by_date = {d: g.set_index("thscode") for d, g in pre["pool"].groupby("date")}
    ind = pre["ind"]
    con_best = pre.get("con_best")

    rows = []
    for r in v.itertuples():
        rec = {"T": r.T, "group": r.group, "close_ret": r.close_ret,
               "open_ret": r.open_ret, "low_ret": r.low_ret,
               "amt_ratio": r.amt_ratio, "score": r.pick_score,
               "buy_triggered": r.buy_triggered, "regime": r.regime}
        try:
            srow = si.loc[(r.thscode, r.T)]
            rec.update(dist_high60=float(srow["dist_high60"]), ret20=float(srow["ret20"]),
                       vol_ratio=float(srow["vol_ratio"]), amount=float(srow["amount"]))
        except KeyError:
            pass
        if r.group == "lu":
            prows = pool_by_date.get(r.T)
            if prows is not None and r.thscode in prows.index:
                pr = prows.loc[r.thscode]
                if isinstance(pr, pd.DataFrame):
                    pr = pr.iloc[0]
                rec.update(lu_time=str(pr["lu_time"]),
                           seal_ratio=float(pr["seal_ratio"]) if pd.notna(pr["seal_ratio"]) else np.nan,
                           cont_cnt=int(pr["cont_cnt"]), theme_cnt=int(pr["theme_cnt"]),
                           # 概念数据缺失（concept_map 缺档/过期 → pool 无 con_* 列）时置 NaN：
                           # 下游 corr_line/bucket 均按「列在且非全空」守卫，NaN 即诚实降级
                           con_pct=float(pr.get("con_pct")) if pd.notna(pr.get("con_pct")) else np.nan,
                           con_lu_cnt=float(pr.get("con_lu_cnt")) if pd.notna(pr.get("con_lu_cnt")) else np.nan)
        else:
            if con_best:
                cb = con_best(r.thscode.split(".")[0], r.T)
                if cb is not None:
                    rec.update(con_pct=float(cb[1]) if cb[1] is not None else np.nan,
                               con_pct5=float(cb[2]) if cb[2] is not None else np.nan,
                               con_lu_cnt=float(cb[3]) if cb[3] is not None else np.nan)
            try:
                ic = store.prim_ind.loc[r.thscode, "ind_code"]
                irow = ind[(ind["date"] == r.T) & (ind["ind_code"] == ic)]
                if not irow.empty:
                    rec.update(ind_rank=float(irow.iloc[0]["ind_rank"]),
                               ind_rank5=float(irow.iloc[0]["ind_rank5"]),
                               ind_lu_cnt=int(irow.iloc[0]["ind_lu_cnt"]))
            except KeyError:
                pass
        rows.append(rec)
    e = pd.DataFrame(rows)

    L = [f"# 轮末因子分析 {run_dir.name}  (n={len(e)})", ""]
    for grp in ("lu", "nlu"):
        g = e[e["group"] == grp]
        if g.empty:
            continue
        tag = "涨停组" if grp == "lu" else "非涨停组"
        L.append(f"## {tag} (n={len(g)})")
        cols = ["score", "dist_high60", "ret20", "vol_ratio", "amount", "seal_ratio",
                "cont_cnt", "theme_cnt", "ind_rank", "ind_rank5", "ind_lu_cnt", "amt_ratio",
                "con_pct", "con_lu_cnt", "con_pct5"]
        L.append("corr(close_ret): " + corr_line(g, cols))
        if grp == "lu":
            specs = [
                ("次日开盘 open_ret", "open_ret", [-10, 0, 2, 5, 8, 100], ["<0", "0~2", "2~5", "5~8", ">8"]),
                ("封单比 seal_ratio", "seal_ratio", [0, .03, .08, .15, 10], ["<3%", "3~8%", "8~15%", ">15%"]),
                ("连板数 cont_cnt", "cont_cnt", [0.5, 1.5, 2.5, 3.5, 20], ["1板", "2板", "3板", "≥4板"]),
                ("题材内涨停 theme_cnt", "theme_cnt", [1.5, 2.5, 4.5, 200], ["2", "3~4", "≥5"]),
                ("驱动概念涨幅 con_pct", "con_pct", [-100, -3, 0, 2, 5, 100], ["<-3", "-3~0", "0~2", "2~5", ">5"]),
                ("概念内涨停 con_lu_cnt", "con_lu_cnt", [-0.5, 0.5, 1.5, 2.5, 3.5, 100], ["缺失/0", "1", "2", "3", "≥4"]),
                ("当日成交额", "amount", [0, 1.5e8, 3e8, 1e10, 1e13], ["<1.5亿", "1.5~3亿", "3~100亿", ">100亿"]),
            ]
            g = g.copy()
            g["lu_hour"] = g["lu_time"].astype(str).str[:2]
            L.append("\n### 涨停时间(小时) 分桶")
            gh = g.groupby("lu_hour")["close_ret"].agg(n="size", **{"win%": lambda s: round((s > 0).mean() * 100, 1), "avg%": "mean"}).round(2)
            L.append(gh.to_string())
        else:
            specs = [
                ("距60日高点 dist_high60", "dist_high60", [0, .86, .90, .95, .99, 1.01], ["<0.86", "0.86~0.90", "0.90~0.95", "0.95~0.99", "≥0.99"]),
                ("20日涨幅 ret20", "ret20", [-100, 0, 10, 20, 35, 200], ["<0", "0~10", "10~20", "20~35", ">35"]),
                ("板块涨幅分位 ind_rank", "ind_rank", [0, .6, .8, .85, 1.01], ["<0.6", "0.6~0.8", "0.8~0.85", "≥0.85"]),
                ("5日板块分位 ind_rank5", "ind_rank5", [0, .6, .8, .85, 1.01], ["<0.6", "0.6~0.8", "0.8~0.85", "≥0.85"]),
                ("板块涨停数 ind_lu_cnt", "ind_lu_cnt", [-0.5, 0.5, 1.5, 2.5, 100], ["0", "1", "2", "≥3"]),
                ("驱动概念涨幅 con_pct", "con_pct", [-100, -3, -1, 0, 2, 100], ["<-3", "-3~-1", "-1~0", "0~2", ">2"]),
                ("概念5日涨幅 con_pct5", "con_pct5", [-100, -5, 0, 5, 100], ["<-5", "-5~0", "0~5", ">5"]),
                ("概念内涨停 con_lu_cnt", "con_lu_cnt", [-0.5, 0.5, 1.5, 2.5, 100], ["缺失/0", "1", "2", "≥3"]),
                ("量比 vol_ratio", "vol_ratio", [0, .5, .8, 1.3, 2.2, 100], ["<0.5", "0.5~0.8", "0.8~1.3", "1.3~2.2", ">2.2"]),
                ("当日成交额", "amount", [0, 4e8, 1e9, 3e9, 1e11], ["<4亿", "4~10亿", "10~30亿", ">30亿"]),
            ]
        for title, col, bins, labels in specs:
            if col in g:
                L.append(f"\n### {title} 分桶")
                L.append(bucket_table(g, col, bins, labels))
        # pick score buckets (min_score calibration)
        L.append("\n### 入选分 score 分桶")
        L.append(bucket_table(g, "score", [0, 55, 60, 65, 70, 100], ["<55", "55~60", "60~65", "65~70", "≥70"]))
        # buy trigger effect
        if "buy_triggered" in g:
            L.append("\n### 买点触发 vs 未触发")
            gt = g.groupby("buy_triggered")["close_ret"].agg(n="size", **{"win%": lambda s: round((s > 0).mean() * 100, 1), "avg%": "mean"}).round(2)
            L.append(gt.to_string())
        # by regime
        L.append("\n### 按市场环境")
        gr = g.groupby("regime")["close_ret"].agg(n="size", **{"win%": lambda s: round((s > 0).mean() * 100, 1), "avg%": "mean"}).round(2)
        L.append(gr.to_string())
        L.append("")

    text = "\n".join(L)
    (run_dir / "analysis.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/mboard_round1_M1")
