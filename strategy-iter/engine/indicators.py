"""Precompute per-stock technical indicators and per-date market state (vectorized)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def limit_band(thscode: str, is_st: bool = False) -> float:
    code = thscode.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if thscode.endswith(".BJ"):
        return 30.0
    if is_st:
        return 5.0
    return 10.0


def build_stock_indicators(store) -> pd.DataFrame:
    """Per (thscode, date) indicators using ONLY data up to that date.
    Uses qfq for trend/momentum, raw for limit flags."""
    q = store.qfq.copy()
    q = q.sort_values(["thscode", "date"])
    g = q.groupby("thscode", sort=False)

    q["ret1"] = g["close"].pct_change() * 100
    for n in (5, 10, 20, 60, 120):
        q[f"ma{n}"] = g["close"].transform(lambda s: s.rolling(n, min_periods=n).mean())
    q["ma20_prev3"] = q.groupby("thscode")["ma20"].shift(3)
    q["vol_ma5"] = g["volume"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    q["amt_ma5"] = g["amount"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    q["amt_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    q["high60"] = g["high"].transform(lambda s: s.rolling(60, min_periods=60).max())
    q["low60"] = g["low"].transform(lambda s: s.rolling(60, min_periods=60).min())
    q["ret20"] = g["close"].transform(lambda s: s.pct_change(20) * 100)
    q["ret5"] = g["close"].transform(lambda s: s.pct_change(5) * 100)
    q["vol_ratio"] = q["volume"] / q["vol_ma5"]
    q["dist_high60"] = q["close"] / q["high60"]
    q["above_low60"] = q["close"] / q["low60"] - 1
    # trend flags
    q["trend_ok"] = (q["close"] > q["ma20"]) & (q["ma20"] > q["ma60"] * 0.995)
    q["ma20_up"] = q["ma20"] > q["ma20_prev3"]
    q["bars"] = g.cumcount() + 1   # bars since first appearance in DB (proxy listing age)

    # merge raw prev_close & limit flags
    r = store.raw[["thscode", "date", "open", "high", "low", "close"]]
    r = r.rename(columns={"open": "r_open", "high": "r_high", "low": "r_low",
                          "close": "r_close"})
    r = r.sort_values(["thscode", "date"])
    r["r_prev_close"] = r.groupby("thscode")["r_close"].shift(1)
    m = q.merge(r, on=["thscode", "date"], how="left")
    m["raw_pct"] = (m["r_close"] / m["r_prev_close"] - 1) * 100
    m["band"] = np.where(m["thscode"].str.startswith(("300", "301")), 20.0,
                 np.where(m["thscode"].str.startswith(("688", "689")), 20.0,
                 np.where(m["thscode"].str.endswith(".BJ"), 30.0, 10.0)))
    m["r_prev_close"] = m["r_prev_close"].round(2)
    limit_price = (m["r_prev_close"] * (1 + m["band"] / 100)).round(2)
    down_price = (m["r_prev_close"] * (1 - m["band"] / 100)).round(2)
    m["is_limit_up"] = m["r_close"] >= limit_price - 0.001
    m["is_limit_down"] = m["r_close"] <= down_price + 0.001
    m["is_yizi"] = m["is_limit_up"] & (m["r_open"] == m["r_high"]) & (m["r_open"] == m["r_low"])
    return m


def build_market_state(store, stock_ind: pd.DataFrame) -> pd.DataFrame:
    """Per-date market breadth / sentiment from full panel + pool files."""
    d = stock_ind
    rows = []
    for date, g in d.groupby("date"):
        n = len(g)
        up = int((g["raw_pct"] > 0).sum())
        down = int((g["raw_pct"] < 0).sum())
        lu = int(g["is_limit_up"].sum())
        ld = int(g["is_limit_down"].sum())
        amt = float(g["amount"].sum())
        rows.append({"date": date, "n_stocks": n, "n_up": up, "n_down": down,
                     "adv_ratio": up / n, "lu_cnt": lu, "ld_cnt": ld,
                     "total_amount": amt})
    ms = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    ms["amt_ma20"] = ms["total_amount"].rolling(20, min_periods=20).mean()
    ms["amt_ratio"] = ms["total_amount"] / ms["amt_ma20"]
    ms["lu_ma5"] = ms["lu_cnt"].rolling(5, min_periods=5).mean()
    ms["adv_ma5"] = ms["adv_ratio"].rolling(5, min_periods=5).mean()

    # max board height from pool files
    pool_h = store.pool.groupby("date")["cont_cnt"].max().rename("max_height")
    ms = ms.merge(pool_h, on="date", how="left")
    ms["max_height"] = ms["max_height"].fillna(0)

    # index momentum (SSE composite)
    idx = store.indices[store.indices["idx_code"] == "000001.SH"][
        ["date", "idx_pct", "idx_pct_5d", "idx_close", "idx_amount"]]
    ms = ms.merge(idx, on="date", how="left")
    idx20 = store.indices[store.indices["idx_code"] == "000001.SH"].copy()
    idx20["idx_pct_20d"] = idx20["idx_close"].pct_change(20) * 100
    ms = ms.merge(idx20[["date", "idx_pct_20d"]], on="date", how="left")

    # yesterday height change (ladder break signal)
    ms["height_chg"] = ms["max_height"].diff()

    # 隔夜美股（单一来源 backend/us_market.py factors.json；C4+ 环境闸门用，
    # 列名简化：us_ndx=纳指综合隔夜 / us_spx=标普500隔夜 / us_sec_up=板块上涨占比）
    us = getattr(store, "us_rows", {}) or {}
    if us:
        us_df = pd.DataFrame([
            {"date": d,
             "us_ndx": r.get("纳斯达克综合"),
             "us_spx": r.get("标普500"),
             "us_dji": r.get("道琼斯"),
             "us_ndx5": r.get("纳斯达克综合5"),
             "us_sec_up": r.get("sec_up")}
            for d, r in us.items()])
        ms = ms.merge(us_df, on="date", how="left")
    else:
        for c in ("us_ndx", "us_spx", "us_dji", "us_ndx5", "us_sec_up"):
            ms[c] = np.nan
    return ms


def _base_regime(row, cfg) -> str:
    """C6/C7 原档位判定（不含任何候选闸门——不加配置时行为与原先逐字节一致）。"""
    lu, adv, ld = row["lu_cnt"], row["adv_ratio"], row["ld_cnt"]
    if pd.isna(row.get("amt_ratio")):
        amt_ratio = 1.0
    else:
        amt_ratio = row["amt_ratio"]
    # forced defensive gates (V2+)
    if ld >= cfg["panic_ld"]:
        return "defensive"
    # 隔夜美股闸门（C4+）：纳指/标普隔夜重挫 -> 隔离外盘系统性风险（恐慌闸门之后生效）
    g_us = cfg.get("us_night_force_defensive")
    if g_us is not None:
        ndx, spx = row.get("us_ndx"), row.get("us_spx")
        if ((ndx is not None and not pd.isna(ndx) and ndx <= g_us[0])
                or (spx is not None and not pd.isna(spx) and spx <= g_us[1])):
            return "defensive"
    g5 = cfg.get("idx5_force_defensive")
    if g5 is not None and not pd.isna(row.get("idx_pct_5d")) and row["idx_pct_5d"] <= g5:
        return "defensive"
    if lu >= cfg["aggressive_lu"] and adv >= cfg["aggressive_adv"] and ld <= cfg["aggressive_ld_max"]:
        return "aggressive"
    if lu < cfg["freeze_lu"] and adv < cfg["freeze_adv"]:
        return "freeze"
    if lu < cfg["defensive_lu"] or adv < cfg["defensive_adv"]:
        return "defensive"
    return "normal"


# 档位下调一档（候选闸门用）：配额随之收紧
# aggressive 2+0 → normal 2+2 → defensive 2+1 → freeze 0+1
_DOWNGRADE = {"aggressive": "normal", "normal": "defensive",
              "defensive": "freeze", "freeze": "freeze"}


def classify_regime(row, cfg) -> str:
    """档位判定；`regime.overheat_downgrade` 配置时叠加「情绪过热降级」。

    候选 A（C15 预注册；默认不配置 → C6/C7 行为不变）：选股日 T 涨停家数显著高于
    前 3 日均值时，视作情绪高潮/兑现阶段，在基础档位上下调一档收紧配额。
    信号只用 ≤T 数据（lu_prev3 = T-1..T-3 均值），无未来函数。
    """
    regime = _base_regime(row, cfg)
    g = cfg.get("overheat_downgrade")
    if g is not None:
        p3 = row.get("lu_prev3")
        if p3 is not None and not pd.isna(p3) and p3 > 0 \
                and row["lu_cnt"] > g["ratio"] * p3:
            regime = _DOWNGRADE.get(regime, regime)
    return regime


def is_overheat(row, cfg) -> bool:
    """情绪过热判定（候选闸门共用）：选股日涨停家数 > 前 3 日均值 × ratio。

    只用 ≤T 数据（lu_prev3 = T-1..T-3 均值，见 add_regime），无未来函数；
    ratio 未配置或前 3 日均值缺失（区间头部/数据缺口）→ False（宁可漏判不误判）。
    """
    g = cfg.get("overheat_downgrade") or cfg.get("overheat_lu_delta")
    if not g:
        return False
    ratio = g.get("ratio", 1.1) if isinstance(g, dict) else 1.1
    p3 = row.get("lu_prev3")
    if p3 is None or pd.isna(p3) or p3 <= 0:
        return False
    return row["lu_cnt"] > ratio * p3


def add_regime(ms: pd.DataFrame, cfg) -> pd.DataFrame:
    ms = ms.copy()
    # 过热降级闸门的前置列：前 3 个交易日涨停家数均值（shift(1) 保证不含当日）
    ms["lu_prev3"] = ms["lu_cnt"].shift(1).rolling(3).mean()
    ms["regime"] = ms.apply(lambda r: classify_regime(r, cfg), axis=1)
    # high-position collapse override: height dropped >=2 while yesterday was high
    prev_h = ms["max_height"].shift(1)
    collapse = (prev_h >= 5) & (ms["height_chg"] <= -2)
    ms.loc[collapse & (ms["regime"] == "aggressive"), "regime"] = "normal"
    return ms
