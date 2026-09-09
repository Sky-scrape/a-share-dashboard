"""Per-day selection logic. Uses ONLY data up to day T."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _lu_time_ok(t: str, late: str) -> bool:
    if not isinstance(t, str) or not t:
        return False
    return t <= late


# 沪深主板代码前缀：沪 600/601/603/605，深 000/001/002/003（中小板已并入深主板）。
# 创业板 300/301、科创板 688/689、北交所 .BJ 不在入选范围。
MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


def in_pick_universe(thscode: str, cfg) -> bool:
    """最终入选范围（仅约束选股结果；研究层统计不受此限，仍用全市场）。"""
    parts = thscode.split(".")
    if len(parts) != 2 or parts[1] not in cfg["exchanges"]:
        return False
    if cfg.get("main_board_only") and not parts[0].startswith(MAIN_BOARD_PREFIXES):
        return False
    return True


def _tier(value, tiers):
    """tiers: list of (threshold_desc...) handled by caller."""
    return 0.0


def _us_tier(usp, cfg):
    """隔夜美股因子分档（C4+）：驱动概念的美股代理组隔夜涨幅 usp（%）。

    >=2% ->1.0 / >=1% ->0.8 / >=0 ->0.6 / >=-1% ->0.35 / <-1% ->0.15；
    无映射或数据缺失 -> us_missing（0.5 中性档，不奖励不惩罚到位）。"""
    if usp is None or (isinstance(usp, float) and np.isnan(usp)):
        return cfg.get("us_missing", 0.5)
    for thr, v in cfg.get("us_link_tiers",
                          [(2.0, 1.0), (1.0, 0.8), (0.0, 0.6), (-1.0, 0.35)]):
        if usp >= thr:
            return v
    return 0.15


def score_lu_row(r, cfg, n_ind_today) -> tuple[float, dict]:
    w = cfg["weights"]
    f = {}
    # theme factor
    f["theme"] = min(max(r["theme_cnt"] - 1, 0) / 5.0, 1.0)
    # concept factor (C1+): 驱动概念热度 = 概念内涨停家数分档, 家数<2 看概念涨跌;
    # 归属/行情缺失给中低档 (不奖励不惩罚到位), 权重缺省时该因子不参与总分
    cp, clu = r.get("con_pct"), r.get("con_lu_cnt")
    if cp is None or (isinstance(cp, float) and np.isnan(cp)):
        f["concept"] = cfg.get("concept_missing", 0.3)
    else:
        clu = 0 if clu is None or (isinstance(clu, float) and np.isnan(clu)) else int(clu)
        if clu >= 2:
            f["concept"] = next((v for thr, v in cfg.get("concept_lu_tiers",
                                 [(4, 1.0), (3, 0.8), (2, 0.6)]) if clu >= thr),
                                cfg.get("concept_pos_floor", 0.4))
        else:
            f["concept"] = cfg.get("concept_pos_floor", 0.4) if cp > 0 \
                else cfg.get("concept_cold", 0.2)
    # ladder factor (M3+: config tiers; legacy default keeps old versions reproducible)
    cc = int(r["cont_cnt"])
    lt = cfg.get("ladder_tiers") or {1: 0.55, 2: 0.9, 3: 1.0, 4: 0.8}
    f["ladder"] = lt.get(cc, 0.6)
    # seal factor
    sr = r.get("seal_ratio")
    if pd.isna(sr):
        f["seal"] = 0.2
    else:
        f["seal"] = 0.15
        for thr, v in cfg["seal_ratio_tiers"]:
            if sr >= thr:
                f["seal"] = v
                break
    # time factor
    f["time"] = 0.05
    for thr, v in cfg["time_tiers"]:
        if r["lu_time"] <= thr:
            f["time"] = v
            break
    # liquidity factor
    amt = r.get("r_amount") or 0.0
    if 3e8 <= amt <= 1e10:
        f["liq"] = 1.0
    elif amt >= 1.5e8:
        f["liq"] = 0.6
    else:
        f["liq"] = 0.3
    # structure factor (volume ratio + high-position penalty)
    vr = r.get("vol_ratio")
    s = 0.5
    if isinstance(vr, float) and not np.isnan(vr):
        for lo, hi, v in cfg["vol_ratio_tiers"]:
            if lo <= vr < hi:
                s = v
                break
    if cc >= 4 and r["theme_cnt"] <= 2:
        s *= 0.5   # high-position acceleration without sector echo
    f["struct"] = s
    # overnight US factor (C4+): weight absent in legacy versions -> not scored
    if "us" in w:
        f["us"] = _us_tier(r.get("us_pct"), cfg)
    total = sum(f[k] * w[k] for k in w)
    return total, f


def select_limit_up(pre, store, date, quota, cfg) -> list[dict]:
    if quota <= 0:
        return []
    pool = pre["pool"]
    day = pool[pool["date"] == date]
    if day.empty:
        return []
    si_idx = pre["si_indexed"]
    st_names = pre["st_name"]
    rows = []
    for r in day.itertuples():
        tc = r.thscode
        if not in_pick_universe(tc, cfg):
            continue
        if r.is_st or r.is_new or tc in st_names:
            continue
        if getattr(r, "is_yizi", False):
            continue
        if cfg["exclude_prev_yizi"] and getattr(r, "prev_yizi", False):
            continue
        if not _lu_time_ok(r.lu_time, cfg["exclude_late_time"]):
            continue
        if (r.r_amount or 0) < cfg["min_amount"]:
            continue
        if r.theme_cnt < cfg["theme_min_members"]:
            continue
        # C1 概念独狼排除: 全部所属概念当日走弱且概念内均无第二家涨停 -> 按独狼板处理;
        # 概念归属/行情缺失时不启用 (诚实降级)
        if cfg.get("exclude_concept_lonewolf"):
            gw, mlu = getattr(r, "con_gate_weak", np.nan), getattr(r, "con_max_lu", np.nan)
            if gw is True and not pd.isna(mlu) and int(mlu) <= 1:
                continue
        try:
            sirow = si_idx.loc[(tc, date)]
        except KeyError:
            continue
        if sirow["bars"] < cfg["min_bars"]:
            continue
        vol_ratio = sirow["vol_ratio"]
        rr = r._asdict()
        rr["vol_ratio"] = vol_ratio
        total, f = score_lu_row(rr, cfg, 0)
        rows.append((total, f, rr))
    rows.sort(key=lambda x: (-x[0], -(x[2].get("seal_ratio") if x[2].get("seal_ratio") == x[2].get("seal_ratio") else 0)))
    picks = []
    for total, f, rr in rows[:quota]:
        if total < cfg["min_score"]:
            break
        picks.append(_make_lu_pick(rr, f, total, store, pre))
    return picks


def _make_lu_pick(rr, f, total, store, pre=None) -> dict:
    tc = rr["thscode"]
    ind_name = ""
    if tc in store.prim_ind.index:
        ind_name = store.prim_ind.loc[tc, "ind_name"]
    cc = int(rr["cont_cnt"])
    con_name = ""
    if pre is not None and rr.get("con_code"):
        con_name = (pre.get("con_name") or {}).get(rr["con_code"], "")
    con_txt = f" 驱动概念[{con_name}]" if con_name else " 概念数据缺失"
    clu_v = rr.get("con_lu_cnt")
    if clu_v is None or (isinstance(clu_v, float) and pd.isna(clu_v)):
        clu_v = 0
    reason = (f"涨停组: {rr['theme']}方向{'/' + con_name if con_name else ''} "
              f"{cc}板 封单比{(rr.get('seal_ratio') or 0)*100:.1f}% 涨停时间{rr['lu_time']}"
              f" 题材内涨停{rr['theme_cnt']}家 概念内涨停{int(clu_v)}家"
              f"{con_txt} 成交{(rr.get('r_amount') or 0)/1e8:.1f}亿")
    return dict(
        group="lu", thscode=tc, name=rr.get("lu_name") or store.name.get(tc, ""),
        industry=ind_name, concept=con_name or None,
        con_pct=rr.get("con_pct"), con_lu_cnt=rr.get("con_lu_cnt"),
        score=float(total), factors=f,
        reason=reason, catalyst=rr.get("lu_reason") or "",
        tech=f"{'/'.join([str(cc)])}连板 换手承接量比{(rr.get('vol_ratio') or 0):.2f}",
        watch=f"次日竞价与开盘承接: 高开幅度观察区间[-2%,+5%]; 开盘涨幅≤+3%且不破昨收-3%视为买点触发",
        watch_range=f"昨收{rr.get('last_price')}",
        risk="高位情绪兑现/次日低开大幅回撤/炸板断板/概念退潮",
        priority=1,
    )


def select_non_lu(pre, store, si_day, ms_row, date, quota, cfg) -> list[dict]:
    if quota <= 0 or si_day.empty:
        return []
    # C7 市场量能闸门：T 日全市场成交额低于 20 日均值的 0.9 倍不低吸
    # （C6 轮末分桶：缩量日 NLU -0.24% n=47 / 放量日 +2.13% n=41，梯度单调；
    #   amt_ratio 缺失时闸门不启用——诚实降级）
    amt_min = cfg.get("market_amt_ratio_min")
    if amt_min is not None:
        v = None
        try:
            v = ms_row["amt_ratio"]
        except (KeyError, TypeError, ValueError):
            pass
        if v is None or pd.isna(v) or v < amt_min:
            return []
    ind = pre["ind"]
    ind_day = ind[ind["date"] == date] if not ind.empty else pd.DataFrame()
    ind_stats = {}
    if not ind_day.empty:
        cols = ["ind_pct", "ind_rank", "ind_rank5", "ind_lu_cnt"]
        if "ind_lu_cnt_prev" in ind_day.columns:
            cols.append("ind_lu_cnt_prev")
        ind_stats = ind_day.set_index("ind_code")[cols].to_dict("index")
    dt_net = pre["dt_recent_netbuy"].get(date, set())
    st_names = pre["st_name"]
    prim = store.prim_ind
    # yesterday limit-up set (V2 exclusion)
    prev_lu_set = set()
    if cfg.get("exclude_prev_lu"):
        pd_ = store.prev_trade_date(date)
        if pd_ is not None and pd_ in store.pool_by_date:
            prev_lu_set = set(store.pool_by_date[pd_]["thscode"])

    ma10_gap_max = cfg.get("ma10_gap_max")
    dist_high_max = cfg.get("dist_high_max", 1.01)

    cands = []
    for r in si_day.itertuples():
        tc = r.thscode
        if not in_pick_universe(tc, cfg):
            continue
        if tc in st_names:
            continue
        if r.bars < cfg["min_bars"]:
            continue
        if getattr(r, "is_limit_up", False) or getattr(r, "is_limit_down", False):
            continue
        if tc in prev_lu_set:
            continue
        amt = r.amount
        if not (amt >= cfg["min_amount"]):
            continue
        if pd.isna(r.amt_ma5) or r.amt_ma5 < cfg["min_amt_ma5"]:
            continue
        if pd.isna(r.raw_pct) or not (cfg["min_raw_pct"] <= r.raw_pct <= cfg["max_raw_pct"]):
            continue
        if pd.isna(r.ret20) or not (cfg["ret20_min"] <= r.ret20 <= cfg["ret20_max"]):
            continue
        if pd.isna(r.dist_high60) or not (cfg["dist_high_min"] <= r.dist_high60 <= dist_high_max):
            continue
        if pd.isna(r.vol_ratio) or not (cfg["vol_ratio_min"] <= r.vol_ratio <= cfg["vol_ratio_max"]):
            continue
        if not (r.trend_ok and r.ma20_up):
            continue
        if ma10_gap_max is not None:
            if pd.isna(r.ma10) or r.ma10 <= 0:
                continue
            gap = r.close / r.ma10 - 1
            if gap < -0.01 or gap > ma10_gap_max:
                continue
        ind_code = prim.loc[tc, "ind_code"] if tc in prim.index else None
        ist = ind_stats.get(ind_code) if ind_code else None
        if ist is None:
            continue
        if cfg.get("require_sector_persistence"):
            lu_today = ist.get("ind_lu_cnt", 0) or 0
            lu_prev = ist.get("ind_lu_cnt_prev", 0) or 0
            if cfg.get("sector_strict"):
                if (ist["ind_rank"] or 0) < 0.6 and lu_today < 1:
                    continue
                active = (lu_today >= 2) or (lu_today >= 1 and ist["ind_rank"] >= 0.85) \
                    or (ist["ind_rank5"] >= 0.85)
            else:
                active = (lu_today >= 1) or (lu_prev >= 1) or (ist["ind_rank5"] >= 0.8)
        else:
            active = (ist["ind_rank"] >= 0.8) or (ist.get("ind_lu_cnt", 0) >= 1) or (ist["ind_rank5"] >= 0.85)
        if not active:
            continue
        # C3: 行业当日涨幅分位下限 —— ind_rank<0.6 桶连续两轮负期望
        # (C1 -1.07% n=16 / C2 -1.88% n=18, 均靠板块涨停家数/5日分位绕过活跃度门槛),
        # 概念定驱动、行业定背景的"行业为辅"防线下, 行业当日明确走弱的不接。
        srm = cfg.get("sector_rank_min")
        if srm is not None and (ist.get("ind_rank") or 0) < srm:
            continue
        # C1 概念维度: 驱动概念 = 所属概念中当日涨幅最高者 (平手取涨停家数)。
        # 概念退潮闸门: 当日与 5 日双弱 -> 回调按下跌中继排除; 数据缺失不启用。
        cb = pre["con_best"](tc.split(".")[0], date) if pre.get("con_best") else None
        if cfg.get("concept_recede_gate") and cb is not None:
            _, cb_pct, cb_pct5, _, _ = cb
            if cb_pct is not None and cb_pct5 is not None and cb_pct < 0 and cb_pct5 < 0:
                continue
        cands.append((r, tc, ind_code, ist, cb))
    if not cands:
        return []

    # sector leader map for bonus
    lead = {}
    for (r, tc, ic, ist, cb) in cands:
        if ic not in lead or r.ret20 > lead[ic][0]:
            lead[ic] = (r.ret20, tc)

    w = cfg["weights"]
    scored = []
    for (r, tc, ic, ist, cb) in cands:
        f = {}
        # sector: config-driven mix (M2+); legacy default keeps old versions reproducible
        sm = cfg.get("sector_mix")
        if sm:
            wr, wr5, wlu, lu_div = sm
            f["sector"] = wr * ist["ind_rank"] + wr5 * ist["ind_rank5"] \
                + wlu * min(ist["ind_lu_cnt"] / lu_div, 1.0)
        else:
            f["sector"] = 0.5 * ist["ind_rank"] + 0.3 * min(ist["ind_lu_cnt"] / 3.0, 1.0) + 0.2 * ist["ind_rank5"]
        # trend
        t = 0.5
        if r.trend_ok and r.ma20_up:
            t = 0.8
        if t > 0.6 and not pd.isna(r.ma10) and r.close > r.ma10:
            t = 1.0
        f["trend"] = t
        # position: deeper pullback zone preferred (V3 pos tiers / M2 pos_tiers)
        dh = r.dist_high60
        pt = cfg.get("pos_tiers")
        if pt is not None:
            f["pos"] = pt[-1][1]
            for thr, v_ in pt:
                if dh < thr:
                    f["pos"] = v_
                    break
        elif cfg.get("sector_strict"):  # V3 pos tiers
            f["pos"] = 1.0 if dh < 0.90 else (0.8 if dh < 0.95 else 0.6)
        else:
            f["pos"] = 1.0 if dh >= 0.92 else (0.7 if dh >= 0.88 else 0.4)
            if r.ret20 > 35:
                f["pos"] *= 0.7
        # momentum sweet spot
        rt = r.ret20
        f["mom"] = 1.0 if 5 <= rt <= 30 else (0.7 if 0 <= rt < 5 or 30 < rt <= 40 else 0.4)
        # liquidity
        f["liq"] = 1.0 if 5e8 <= r.amount <= 8e9 else 0.6
        # recognition (V2: primary factor; C2+ 概念直接阶梯: 概念内涨停家数是主梯度
        #   —— C1 实测 概念<=2家桶 -1.37% (n=46, 四个涨幅桶全负) vs >=3家 +0.92% (n=116);
        #   阶梯替换 max 吸收, 概念冷的票不再靠龙虎榜顶上来。概念数据缺失回退原口径。
        #   C1 的 recog_concept_first (max 口径) 保留用于旧版本复现。)
        rec = None
        tiers = cfg.get("recog_concept_tiers")
        if tiers and cb is not None:
            cb_lu = cb[3]
            if cb_lu is not None and not pd.isna(cb_lu):
                rec = next((v_ for thr, v_ in tiers if int(cb_lu) >= thr), tiers[-1][1])
        if rec is None:
            rec = 0.3
            if tc in dt_net:
                rec = 1.0
            elif lead.get(ic, (0, ""))[1] == tc:
                rec = 0.85
            elif ist.get("ind_lu_cnt", 0) >= 2:
                rec = 0.6
            if cfg.get("recog_concept_first") and cb is not None:
                _, cb_pct, _, cb_lu, _ = cb
                if cb_lu is not None and not pd.isna(cb_lu) and cb_lu >= 2:
                    rec = max(rec, 1.0)
                elif cb_lu == 1:
                    rec = max(rec, 0.8)
                elif cb_pct is not None and not pd.isna(cb_pct) and cb_pct > 0:
                    rec = max(rec, 0.6)
        f["recog"] = rec
        # overnight US factor (C4+): weight absent in legacy versions -> not scored
        if "us" in w:
            usp = pre["us_proxy_at"](date, cb[0]) if (cb and pre.get("us_proxy_at")) else None
            f["us"] = _us_tier(usp, cfg)
        total = sum(f[k] * w[k] for k in w)
        scored.append((total, f, r, tc, ic, ist, cb))

    scored.sort(key=lambda x: -x[0])
    picks = []
    seen_ind = []
    for total, f, r, tc, ic, ist, cb in scored:
        if total < cfg["min_score"]:
            break
        if ic in seen_ind and len(picks) >= 1:
            continue   # diversify across sectors
        seen_ind.append(ic)
        picks.append(_make_nlu_pick(r, tc, ic, ist, f, total, store, cb, pre))
        if len(picks) >= quota:
            break
    return picks


def _make_nlu_pick(r, tc, ic, ist, f, total, store, cb=None, pre=None) -> dict:
    ind_name = store.prim_ind.loc[tc, "ind_name"] if tc in store.prim_ind.index else ""
    con_name = ""
    if cb is not None and pre is not None:
        con_name = (pre.get("con_name") or {}).get(cb[0], "")
    con_txt = f"驱动概念[{con_name}]" if con_name else "概念数据缺失"
    reason = (f"非涨停组: 趋势结构多头(MA20>MA60且上行), 处于活跃板块[{ind_name}]"
              f"(当日涨幅分位{ist['ind_rank']*100:.0f}%, 板块涨停{ist['ind_lu_cnt']}家)"
              f" {con_txt}"
              f" 距60日高点{(1-r.dist_high60)*100:.1f}% 20日涨幅{r.ret20:.1f}%")
    return dict(
        group="nlu", thscode=tc, name=store.name.get(tc, ""),
        industry=ind_name, concept=con_name or None,
        con_pct=(cb[1] if cb else None), con_lu_cnt=(cb[3] if cb else None),
        score=float(total), factors=f,
        reason=reason, catalyst=f"板块[{ind_name}]轮动延续/资金承接/概念热度",
        tech=f"收盘{r.close:.2f} MA20上方 量比{r.vol_ratio:.2f}",
        watch="次日开盘[-2%,+3%]为观察区间; 收盘站上max(开盘,昨收)视为买点触发",
        watch_range=f"昨收{r.close:.2f}",
        risk="板块退潮补跌/概念退潮/资金承接不足/持续阴跌",
        priority=1,
    )
