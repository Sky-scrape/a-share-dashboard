"""One-time per-round precompute: stock indicators, market state, industry stats, enriched pool."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import build_stock_indicators, build_market_state, add_regime


def theme_key(reason: str) -> str:
    if not isinstance(reason, str) or not reason:
        return ""
    first = reason.split("+")[0].strip()
    return first


def build_concept_tables(store):
    """概念热度表（概念维度单一来源计算，C 线一阶因子）。

    返回 (con_day, con_by_stock, best_at, gate_at)：
    - con_day: {(con_code, date): dict(pct, pct5, rank, lu_cnt)}——rank 为当日
      概念涨幅在全部有值概念中的升序百分位（与行业 ind_rank 同口径）；
      lu_cnt 为该概念当日全市场涨停家数（研究层口径，含创业板/科创板）。
    - con_by_stock: {6位代码: [con_code...]}（membership 经目录换码，剔除机械概念）。
    - best_lookup: {(6位代码, date): (con_code, pct, pct5, lu_cnt, rank)}——个股驱动
      概念 = 所属概念中当日涨幅最高者（平手取涨停家数更高者，与 speculate._best_concept
      同口径）。数据缺失（无归属/概念当日无值）时无该键，消费方诚实降级。
    """
    empty = ({}, {}, {}, {})
    mem = store.con_membership
    cat = store.con_catalog
    if not mem or not cat or store.con_daily.empty:
        return empty
    name_by_code = {}
    for name, code in cat.items():
        name_by_code[code.split(".")[0]] = name
    by_stock = {}
    for code6, names in mem.items():
        codes = [cat[n] for n in names if n in cat]
        if codes:
            by_stock[code6] = codes
    if not by_stock:
        return empty

    cd = store.con_daily
    # 当日概念涨幅分位（有值概念内升序百分位，平分长度，与 _rotation_strength._rank 同口径）
    cd = cd.copy()
    cd["con_rank"] = cd.groupby("date")["con_pct"].rank(pct=True, ascending=True)
    # 概念内涨停家数：全市场涨停池 × 概念归属（研究层不收窄）
    lu = store.pool[["date", "thscode"]].dropna(subset=["thscode"])
    lu["code6"] = lu["thscode"].str.split(".").str[0]
    lu = lu[lu["code6"].isin(by_stock)]

    def codes_of(code6):
        return by_stock.get(code6) or []

    # explode 概念归属
    lu2 = lu.assign(con_code=lu["code6"].map(codes_of)).explode("con_code")
    lu_cnt = lu2.dropna(subset=["con_code"]).groupby(["con_code", "date"]).size()

    con_day = {}
    for (code, date), row in cd.set_index(["con_code", "date"]).iterrows():
        con_day[(code, date)] = {
            "pct": None if pd.isna(row["con_pct"]) else float(row["con_pct"]),
            "pct5": None if pd.isna(row["con_pct_5d"]) else float(row["con_pct_5d"]),
            "rank": None if pd.isna(row["con_rank"]) else float(row["con_rank"]),
            "lu_cnt": int(lu_cnt.get((code, date), 0)),
        }

    def best_at(code6, date):
        codes = by_stock.get(code6)
        if not codes:
            return None
        best, best_key = None, (-999.0, -1)
        for c in codes:
            d = con_day.get((c, date))
            if d is None or d["pct"] is None:
                continue
            key = (d["pct"], d["lu_cnt"])
            if key > best_key:
                best_key, best = key, (c, d)
        if best is None:
            return None
        c, d = best
        return (c, d["pct"], d["pct5"], d["lu_cnt"], d["rank"])

    def gate_at(code6, date):
        """概念独狼闸门字段：(全部概念走弱, 概念内最大涨停家数)。

        无任何概念当日有值时返回 None（数据缺失，闸门不启用——诚实降级）。"""
        codes = by_stock.get(code6)
        if not codes:
            return None
        pcts, max_lu = [], 0
        for c in codes:
            d = con_day.get((c, date))
            if d is None or d["pct"] is None:
                continue
            pcts.append(d["pct"])
            max_lu = max(max_lu, d["lu_cnt"])
        if not pcts:
            return None
        return (all(p < 0 for p in pcts), max_lu)

    return con_day, by_stock, best_at, gate_at


def precompute(store, cfg) -> dict:
    """Return dict of everything the selector/validator needs."""
    out = {}
    si = build_stock_indicators(store)
    out["si"] = si

    ms = build_market_state(store, si)
    ms = add_regime(ms, cfg["regime"])
    out["ms"] = ms

    # industry per-date stats
    ind = store.ind_daily.copy()
    if not ind.empty and not store.pool.empty and not store.prim_ind.empty:
        pool = store.pool[["date", "thscode"]].merge(
            store.prim_ind.reset_index()[["thscode", "ind_code"]], on="thscode", how="left")
        lu_cnt = pool.dropna(subset=["ind_code"]).groupby(["date", "ind_code"]).size().rename("ind_lu_cnt")
        ind = ind.merge(lu_cnt, on=["date", "ind_code"], how="left")
        ind["ind_lu_cnt"] = ind["ind_lu_cnt"].fillna(0).astype(int)
        ind["ind_rank"] = ind.groupby("date")["ind_pct"].rank(pct=True, ascending=True)
        ind["ind_rank5"] = ind.groupby("date")["ind_pct_5d"].rank(pct=True, ascending=True)
        ind = ind.sort_values(["ind_code", "date"])
        ind["ind_lu_cnt_prev"] = ind.groupby("ind_code")["ind_lu_cnt"].shift(1)
    out["ind"] = ind

    # enriched limit-up pool
    pool = store.pool.copy()
    pool["theme"] = pool["lu_reason"].apply(theme_key)
    theme_cnt = pool.groupby(["date", "theme"])["thscode"].transform("count")
    pool["theme_cnt"] = theme_cnt
    # yizi flag & prev-yizi from raw panel
    rawm = store.raw[["thscode", "date", "open", "high", "low", "close", "amount"]]
    rawm = rawm.rename(columns={c: "r_" + c for c in ["open", "high", "low", "close", "prev_close", "amount"]})
    pool = pool.merge(rawm, on=["thscode", "date"], how="left")
    pool["is_yizi"] = (pool["r_open"] == pool["r_high"]) & (pool["r_open"] == pool["r_low"]) & (pool["r_open"] == pool["r_close"])
    pool["seal_ratio"] = pool["seal_money"] / pool["r_amount"]
    # previous trade day yizi
    td = store.trade_dates
    prev_map = {td[i]: td[i - 1] for i in range(1, len(td))}
    pool["prev_date"] = pool["date"].map(prev_map)
    yizi_map = set(map(tuple, rawm.assign(yizi=(rawm["r_open"] == rawm["r_high"]) & (rawm["r_open"] == rawm["r_low"]) & (rawm["r_open"] == rawm["r_close"]))
                     .loc[lambda d: d["yizi"], ["thscode", "date"]].values.tolist()))
    pool["prev_yizi"] = [ (tc, pd) in yizi_map for tc, pd in zip(pool["thscode"], pool["prev_date"]) ]
    # bars (listing age proxy)
    bars = si.groupby("thscode")["bars"].apply(lambda s: s)  # keep si lookup
    out["pool"] = pool

    # stock name map with ST check
    nm = store.name
    out["st_name"] = {tc for tc, n in nm.items() if isinstance(n, str) and "ST" in n.upper()}

    # dragon-tiger recent net buy lookup: per date set of thscodes with net>0 in last 5 trade days
    dt_recent = {}
    if not store.dt.empty:
        dt = store.dt.copy()
        dt["net_value"] = pd.to_numeric(dt["net_value"], errors="coerce").fillna(0)
        net = dt.groupby(["date", "thscode"])["net_value"].sum().reset_index()
        for i, d in enumerate(td):
            window = td[max(0, i - 5):i]  # last 5 trade days BEFORE d
            sub = net[net["date"].isin(window)]
            agg = sub.groupby("thscode")["net_value"].sum()
            dt_recent[d] = set(agg[agg > 0].index)
    out["dt_recent_netbuy"] = dt_recent

    # industry leader lookup: per date, per ind_code: thscode with best ret20 (from si)
    out["si_indexed"] = si.set_index(["thscode", "date"])

    # ---- 概念维度（C 线一阶因子）：热度表 + 池行驱动概念挂接 ----
    con_day, con_by_stock, con_best, con_gate = build_concept_tables(store)
    out["con_day"] = con_day
    out["con_by_stock"] = con_by_stock
    out["con_best"] = con_best
    out["con_gate"] = con_gate
    out["con_name"] = {v: k for k, v in store.con_catalog.items()}

    pool = out["pool"]
    if con_best:   # 概念数据缺失时 build_concept_tables 降级返回 {}，truthy 判断而非 is not None
        vals = [con_best(str(tc).split(".")[0], d) for tc, d in
                zip(pool["thscode"], pool["date"])]
        pool["con_code"] = [v[0] if v else None for v in vals]
        pool["con_pct"] = [v[1] if v else np.nan for v in vals]
        pool["con_pct5"] = [v[2] if v else np.nan for v in vals]
        pool["con_lu_cnt"] = [v[3] if v else np.nan for v in vals]
        pool["con_rank"] = [v[4] if v else np.nan for v in vals]
        gates = [con_gate(str(tc).split(".")[0], d) for tc, d in
                 zip(pool["thscode"], pool["date"])]
        pool["con_gate_weak"] = [g[0] if g else np.nan for g in gates]
        pool["con_max_lu"] = [g[1] if g else np.nan for g in gates]
    return out
