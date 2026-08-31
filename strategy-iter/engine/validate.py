"""T+1 validation, scoring and attribution. Uses ONLY T+1 data for outcomes."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _ret_pct(a, b):
    if a is None or b is None or pd.isna(a) or pd.isna(b) or b == 0:
        return np.nan
    return (a / b - 1) * 100


def validate_pick(pick, store, pre, T, T1, cfg) -> dict | None:
    """Return validation row or None if T+1 data missing (suspension)."""
    tc = pick["thscode"]
    try:
        q0 = pre["si_indexed"].loc[(tc, T)]
        q1 = pre["si_indexed"].loc[(tc, T1)]
    except KeyError:
        return None
    c0 = q0["close"]
    o1, h1, l1, c1 = q1["open"], q1["high"], q1["low"], q1["close"]
    if pd.isna(c1) or pd.isna(o1):
        return None
    open_ret = _ret_pct(o1, c0)
    high_ret = _ret_pct(h1, c0)
    low_ret = _ret_pct(l1, c0)
    close_ret = _ret_pct(c1, c0)
    amt0, amt1 = q0["amount"], q1["amount"]
    amt_ratio = (amt1 / amt0) if amt0 and amt0 > 0 and amt1 is not None else np.nan

    # sector & index on T+1
    ind_pct = np.nan
    ic = store.prim_ind.loc[tc, "ind_code"] if tc in store.prim_ind.index else None
    if ic is not None and not pre["ind"].empty:
        sub = pre["ind"][(pre["ind"]["date"] == T1) & (pre["ind"]["ind_code"] == ic)]
        if not sub.empty:
            ind_pct = sub.iloc[0]["ind_pct"]
    # concept on T+1 (C1+): 强于所属概念 + 概念持续性归因用
    con_pct1 = np.nan
    con_best = pre.get("con_best")
    if con_best:
        cb1 = con_best(tc.split(".")[0], T1)
        if cb1 is not None and cb1[1] is not None:
            con_pct1 = cb1[1]
    idx_pct = np.nan
    sub = store.indices[(store.indices["date"] == T1) & (store.indices["idx_code"] == "000001.SH")]
    if not sub.empty:
        idx_pct = sub.iloc[0]["idx_pct"]

    grp = pick["group"]
    bcfg = cfg["buys"]["lu" if grp == "lu" else "nlu"]
    in_window = (bcfg["open_min"] <= open_ret <= bcfg["open_max"]) if not pd.isna(open_ret) else False
    if grp == "lu":
        triggered = (not pd.isna(open_ret) and open_ret <= bcfg["trigger_open_max"]
                     and not pd.isna(low_ret) and low_ret >= bcfg["trigger_low_min"]
                     and c1 > o1)
    else:
        tmin = bcfg.get("trigger_open_min")
        tlow = bcfg.get("trigger_low_min")
        triggered = (not pd.isna(open_ret) and c1 > max(o1, c0))
        if tmin is not None:
            triggered = triggered and open_ret >= tmin
        if tlow is not None:
            triggered = triggered and (not pd.isna(low_ret)) and low_ret >= tlow
    risk_hit = (not pd.isna(low_ret) and low_ret <= bcfg["risk_low"]) or \
               (not pd.isna(close_ret) and close_ret <= bcfg["risk_close"])

    win = (not pd.isna(close_ret)) and close_ret > 0
    strong_sector = (not pd.isna(ind_pct)) and close_ret > ind_pct
    strong_index = (not pd.isna(idx_pct)) and close_ret > idx_pct
    # 概念数据缺失记 NaN 而非 False：_grp_stats 的 notna 守卫+skipna mean 依赖它区分
    # 「缺失」与「跑输」（strong_sector 无此区分是 M 线既有口径，对比基准不动）
    strong_concept = (close_ret > con_pct1
                      if (not pd.isna(con_pct1)) and (not pd.isna(close_ret)) else np.nan)

    sc = cfg["scoring"]
    result = 50 + close_ret * sc["close_w"] / 100 * 10 + high_ret * sc["high_w"] / 100 * 10 \
        - max(0.0, (-low_ret / 100) - sc["low_pen_start"]) * sc["low_pen"]
    # simpler & bounded:
    result = 50 + close_ret * 6 + max(high_ret, 0) * 2 - max(0.0, -low_ret - 3) * 3
    result = float(np.clip(result, 0, 100))

    if grp == "lu":
        expects = [close_ret > 0, strong_sector, strong_index]
        prem = (not pd.isna(open_ret)) and open_ret > 0
        logic = 100.0 * (sum(bool(e) for e in expects) + (0.5 if prem else 0)) / 3.5
    else:
        repair = (not pd.isna(close_ret)) and close_ret > max(low_ret + 1.5, 0)
        expects = [repair or (close_ret > 0), strong_sector, strong_index]
        logic = 100.0 * sum(bool(e) for e in expects) / 3.0
    logic = float(np.clip(logic, 0, 100))

    if triggered and not risk_hit:
        execution = 100.0
    elif triggered and risk_hit:
        execution = 60.0
    elif in_window:
        execution = 70.0
    else:
        execution = 40.0

    gap = abs(open_ret) if not pd.isna(open_ret) else 0.0
    intra = abs(close_ret - open_ret) if not (pd.isna(close_ret) or pd.isna(open_ret)) else abs(close_ret)
    luck = float(np.clip(100 * gap / (gap + intra + 0.3), 0, 100))

    comp = 0.4 * result + 0.3 * logic + 0.2 * execution + 0.1 * (100 - luck)
    rating = "A" if comp >= 75 else "B" if comp >= 60 else "C" if comp >= 45 else "D"

    attribution = _attribute(grp, open_ret, close_ret, low_ret, idx_pct, ind_pct,
                             con_pct1, amt_ratio, triggered, bcfg)
    logic_realized = logic >= 60

    return dict(
        T=T, T1=T1, group=grp, thscode=tc, name=pick["name"], industry=pick["industry"],
        concept=pick.get("concept"), con_pct1=con_pct1,
        pick_score=pick["score"],
        open_ret=open_ret, high_ret=high_ret, low_ret=low_ret, close_ret=close_ret,
        amt_ratio=amt_ratio, ind_pct=ind_pct, idx_pct=idx_pct,
        in_window=in_window, buy_triggered=triggered, risk_hit=risk_hit,
        win=win, strong_sector=strong_sector, strong_index=strong_index,
        strong_concept=strong_concept,
        result_score=result, logic_score=logic, execution_score=execution,
        luck_score=luck, composite=comp, rating=rating,
        logic_realized=logic_realized, attribution=attribution,
    )


def _attribute(grp, open_ret, close_ret, low_ret, idx_pct, ind_pct, con_pct1,
               amt_ratio, triggered, bcfg) -> str:
    o, c, l = open_ret, close_ret, low_ret
    if pd.isna(c):
        return "数据缺失"
    if not pd.isna(idx_pct) and idx_pct <= -1.0 and c < 0:
        return "市场系统性风险"
    if not triggered and abs(c) < 2.5:
        return "买点未触发"
    if grp == "lu" and not pd.isna(o) and o >= 3.0 and c <= o - 3.0:
        return "情绪兑现/高开回落"
    if not pd.isna(o) and o > bcfg.get("trigger_open_max", 3.0) and c < 0:
        return "买点过高"
    if not pd.isna(ind_pct) and ind_pct <= -0.5 and c < 1.0:
        return "板块持续性不足"
    if not pd.isna(con_pct1) and con_pct1 <= -0.5 and c < 1.0:
        return "概念持续性不足"
    if not pd.isna(l) and l <= -4.0:
        return "资金承接不足"
    if not pd.isna(amt_ratio) and amt_ratio < 0.55 and c < 0:
        return "资金承接不足"
    if abs(c) < 1.5:
        return "随机波动"
    if c >= 1.5:
        return "正常兑现"
    return "个股辨识度不够/其他"
