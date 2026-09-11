# -*- coding: utf-8 -*-
"""验证/优化器/样本外/观察项闭环：只读写 pool_track.json 与 pool_opt.json（回填幂等，优化器状态是验证记录的纯函数）。"""
import datetime as _dt
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import execution_layer  # noqa: E402
import fsutil  # noqa: E402  原子写盘单一来源（backend/fsutil.py）
import snapio  # noqa: E402
from spec_duckdb import ohlc_rets  # noqa: E402
from spec_rules import _date_iso  # noqa: E402
from spec_series import (_industry_lookup, _series_pct, index_gain,  # noqa: E402
                         index_series, industry_series)

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_TRACK = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                          "data", "recap", "pool_track.json")   # 每日验证记录（按验证日覆盖写）
POOL_OPT = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "data", "recap", "pool_opt.json")       # 优化器状态（验证记录的纯函数）

# M_Final 执行层（冻结）：买点/风险位判定口径（与最终筛选方案-主板.md 一致）。
# 验证用，不随优化器变化——优化器只调选股侧，不调执行侧。
# 常量单一来源 backend/execution_layer.py（竞价页执行卡共用同一份窗口）。
_MF_BUY = execution_layer.MF_BUY
# 优化器有界旋钮：门槛上下限与惩罚上限（结构规则永不自动改动）
_OPT_MIN_SCORE = {"lu": (60.0, 75.0), "nlu": (60.0, 75.0)}
_OPT_BASELINE_MINSCORE = {"lu": 70.0, "nlu": 70.0}   # C7 固化门槛基线（expB 证据），优化器只在此基础上抬升
_OPT_WINDOW_DAYS = 45      # 滚动统计窗口（自然日）
_OPT_BUCKET_MIN_N = 8      # 分桶惩罚最少样本
_OPT_MINSCORE_MIN_N = 20   # 门槛调整最少样本

# ---------------------------------------------------------------- 每日验证与自我优化
# 闭环：T-1 日盘后选出 T 日关注标的 → T 日收盘后用真实走势验证（买点/风险位/归因）
# → 滚动窗口统计 → 有界机械调参（因子分桶惩罚 + 入选门槛微调）。
# 纪律（对应《自动选股与策略自迭代系统.md》）：结构规则/环境配额/执行层永不自动改动；
# 优化器状态是验证记录的纯函数（可重放、回填幂等，不做棘轮累积）；全部调整留痕。
# 单日结果不直接改规则——一切调整都来自 ≥45 日窗口的聚合统计，且样本不足时不动作。

def _load_opt():
    """读优化器状态（pool_opt.json）；缺失/损坏/旧线版本回默认（结构规则不受影响）。

    C7（2026-09-09）起版本门收紧到 C7 前缀：旧 C_Final 线旋钮（门槛 55）不带入——
    C7 入选门槛基准 70（expB 实验证据：60~70 分区间 5 只全亏、>=70 占 98%），
    优化器界限同步放宽到 60~75。"""
    base = {"version": "C7_Final", "updated": None,
            "min_score": {"lu": 70.0, "nlu": 70.0}, "penalties": {"lu": {}, "nlu": {}},
            "oos_reviews": [], "log": []}
    try:
        with open(POOL_OPT, encoding="utf-8") as f:
            d = json.load(f)
        ver = str(d.get("version") or "")
        if not ver.startswith("C7"):
            return base   # C_Final 旧线旋钮不带入 C7 线
        base["version"] = ver
        base["updated"] = d.get("updated")
        ms = d.get("min_score") or {}
        base["min_score"] = {"lu": float(ms.get("lu") or 70.0),
                             "nlu": float(ms.get("nlu") or 70.0)}
        pn = d.get("penalties") or {}
        base["penalties"] = {"lu": dict(pn.get("lu") or {}), "nlu": dict(pn.get("nlu") or {})}
        base["oos_reviews"] = list(d.get("oos_reviews") or [])
    except Exception:  # noqa: BLE001 - 无状态文件=默认口径
        pass
    return base


def _pick_buckets(f):
    """因子 → 可惩罚分桶键（与 M3 轮末分析口径一致；优化器与验证共用）。"""
    out = []
    if not f:
        return out
    if f.get("group") == "lu":
        ft = str(f.get("ft") or "99:99")
        if ft > "11:30":
            out.append("time_late")        # 13:30 后封板（M1 实测 +0.39%/-0.25%）
        r = f.get("seal_ratio") or 0
        if r < 0.08:
            out.append("seal_low")         # 封单比 <8%
        lb = int(f.get("lb") or 1)
        if lb == 1:
            out.append("ladder_1")         # 首板（M3 +2.42% 全组最弱档）
        tc = int(f.get("tc") or 2)
        if tc <= 2:
            out.append("theme_2")          # 题材内仅 2 家
        if not f.get("sealed"):
            out.append("struct_reopen")    # 盘中开过板
    else:
        rh = f.get("ratio60") or 0
        if rh >= 0.90:
            out.append("pos_high")         # 贴近 0.95 上限（甜区内偏高位）
        g20 = f.get("g20")
        if g20 is None or g20 < 5:
            out.append("mom_low")          # 20 日涨幅 <5%
        if not f.get("sec"):
            out.append("sector_none")      # 板块代理全缺
        if (f.get("amount") or 0) > 8e9:
            out.append("liq_big")          # 流动性 6 分档（>80 亿）
        if not f.get("net"):
            out.append("recog_none")       # 无龙虎榜净买
        if f.get("concept_lu") is not None and f.get("concept_lu") < 3:
            out.append("concept_cold")     # 概念温吞（概念内涨停<3家，C_Final 主梯度弱档）
    return out


def _val_attribute(grp, o, c, l, amtr, triggered, b, ind_pct=None, con_pct=None):
    """归因（口径对齐 strategy-iter 验证器：板块/概念持续性不足 = 当日板块/概念指数
    ≤-0.5% 且个股未涨；概念维度 C 线新增，排在板块之后、资金承接之前）。"""
    if c is None:
        return "停牌/数据缺失"
    if not triggered and abs(c) < 2.5:
        return "买点未触发"
    if grp == "lu" and o is not None and o >= 3.0 and c <= o - 3.0:
        return "情绪兑现/高开回落"
    if o is not None and o > b["win_max"] and c < 0:
        return "买点过高"
    if ind_pct is not None and ind_pct <= -0.5 and c < 1.0:
        return "板块持续性不足"
    if con_pct is not None and con_pct <= -0.5 and c < 1.0:
        return "概念持续性不足"
    if l is not None and l <= b["risk_low"]:
        return "资金承接不足"
    if amtr is not None and amtr < 0.55 and c < 0:
        return "资金承接不足"
    if abs(c) < 1.5:
        return "随机波动"
    if c >= 1.5:
        return "正常兑现"
    return "个股辨识度不够/其他"


def validate_prev_pool(date8, sent_rows, concept_pct=None):
    """验证上一交易日备选池：昨日选的票今天是否成功（买点/风险位/归因）。

    concept_pct：{概念名: 当日概念指数涨幅%}（T+1 快照 concepts 模块，调用方传入），
    用于 strong_concept（T+1 收盘强于所属概念指数，引擎 C 线同口径）与
    「概念持续性不足」归因；缺失（概念数据断链/旧线 picks 无 concept 因子）时
    置 None——区分「缺失」与「跑输」，与引擎 notna 守卫同纪律。
    返回 payload；无上一交易日或其快照无备选池时 ok=False（仍留痕，让优化器知道空窗）。
    旧 V3 口径的昨日 picks（无 factors）照常验证结果、归因，但不参与优化器分桶学习
    （rules=v3/mf/cf 标记，优化器只学 C7 口径样本）。
    """
    iso = _date_iso(date8)
    prevs = [r["date"] for r in sent_rows if (r["date"] or "") < iso]
    if not prevs:
        return None
    prev8 = prevs[-1].replace("-", "")
    try:
        snap = snapio.load(prev8)
    except Exception:  # noqa: BLE001
        snap = None
    mod = ((snap or {}).get("modules") or {}).get("speculation") or {}
    pool = (mod.get("data") or {}).get("pool") or {}
    picks = pool.get("picks") or []
    note0 = str(pool.get("note") or "")
    rules_tag = ("c7" if note0.startswith("C7")
                 else "cf" if "C_Final" in note0
                 else "mf" if "M_Final" in note0 else "v3")
    if not picks:
        return {"ok": False, "date": prev8, "rules": rules_tag, "picks": [],
                "note": "上一交易日快照无备选池标的（冰点或模块缺失）"}
    try:
        ohlc = ohlc_rets(date8, [str(p.get("代码") or "") for p in picks])
    except Exception:  # noqa: BLE001
        ohlc = {}
    try:
        idx1 = index_gain(index_series("SH", date8), iso, 1)
    except Exception:  # noqa: BLE001
        idx1 = None
    ind_map = _industry_lookup()   # {code6: (881代码, 行业名)}；映射/目录缺失时板块对比诚实置空
    out = []
    for p in picks:
        code = str(p.get("代码") or "")
        grp = "nlu" if p.get("类型") == "非涨停板" else "lu"
        b = _MF_BUY[grp]
        r = ohlc.get(code) or {}
        o, h, l, c = r.get("o"), r.get("h"), r.get("l"), r.get("c")
        row = {"code": code, "name": p.get("名称"), "group": grp,
               "pick_score": p.get("得分"), "rules": rules_tag,
               "buckets": _pick_buckets(p.get("factors")),
               "env": pool.get("env")}
        if c is None or o is None:
            row.update({"attribution": "停牌/数据缺失"})
            out.append(row)
            continue
        in_window = bool(b["win_min"] <= o <= b["win_max"])
        if grp == "lu":
            triggered = in_window and l is not None and l >= b["low_min"] and c > o
        else:
            # 低吸触发：开盘在窗内且收盘站上 max(开盘, 昨收)（昨收涨幅=0）
            triggered = in_window and c > max(o, 0.0)
        risk_hit = (l is not None and l <= b["risk_low"]) or (c <= b["risk_close"])
        win = c > 0
        ind = ind_map.get(code)
        ind_pct = None
        if ind:
            try:
                ind_pct = _series_pct(industry_series(ind[0], date8), iso)
            except Exception:  # noqa: BLE001 - 行业序列取不到时板块对比置空
                ind_pct = None
        attr = ("市场系统性风险"
                if (idx1 is not None and idx1 <= -1.0 and c < 0)
                else _val_attribute(grp, o, c, l, r.get("amtr"), triggered, b,
                                    ind_pct,
                                    (concept_pct or {}).get((p.get("factors") or {}).get("concept"))))
        con_pct1 = (concept_pct or {}).get((p.get("factors") or {}).get("concept"))
        row.update({"open": o, "high": h, "low": l, "close": c,
                    "amt_ratio": r.get("amtr"), "in_window": in_window,
                    "buy_triggered": triggered, "risk_hit": risk_hit,
                    "win": win, "strong_index": (idx1 is not None and c > idx1),
                    "industry": ind[1] if ind else None, "ind_pct": ind_pct,
                    "strong_sector": (ind_pct is not None and c > ind_pct),
                    "concept": (p.get("factors") or {}).get("concept"),
                    "con_pct1": con_pct1,
                    # None=概念数据缺失（不判）；True/False=强于/弱于所属概念指数
                    "strong_concept": (c > con_pct1 if (con_pct1 is not None) else None),
                    "attribution": attr})
        out.append(row)
    return {"ok": True, "date": prev8, "env": pool.get("env"), "rules": rules_tag,
            "picks": out, "stats": {"overall": _val_stats(out),
                                    "lu": _val_stats([p for p in out if p["group"] == "lu"]),
                                    "nlu": _val_stats([p for p in out if p["group"] == "nlu"]),
                                    "exec": _val_exec_stats(out)},
            "note": None}


def _val_stats(rows):
    done = [p for p in rows if p.get("close") is not None]
    if not done:
        return {"n": len(rows), "valid": 0}
    cr = [p["close"] for p in done]

    def pct(f):
        return round(100.0 * sum(1 for p in done if f(p)) / len(done), 1)
    attr = {}
    for p in done:
        attr[p.get("attribution") or "-"] = attr.get(p.get("attribution") or "-", 0) + 1
    sec = [p for p in done if p.get("ind_pct") is not None]
    con = [p for p in done if p.get("strong_concept") is not None]
    return {"n": len(rows), "valid": len(done),
            "win_rate": pct(lambda p: p["close"] > 0),
            "avg_close": round(sum(cr) / len(cr), 2),
            "avg_open": round(sum(p["open"] for p in done) / len(done), 2),
            "buy_trigger_rate": pct(lambda p: p.get("buy_triggered")),
            "risk_hit_rate": pct(lambda p: p.get("risk_hit")),
            "strong_sector_rate": (round(100.0 * sum(1 for p in sec if p.get("strong_sector")) / len(sec), 1)
                                   if sec else None),
            "sector_n": len(sec),
            "strong_concept_rate": (round(100.0 * sum(1 for p in con if p.get("strong_concept")) / len(con), 1)
                                    if con else None),
            "concept_n": len(con),
            "attribution": dict(sorted(attr.items(), key=lambda x: -x[1]))}


def _exec_pnl_realtime(grp, o, h, l, c):
    """实时可执行近似收益（%）；与 strategy-iter backtest_capital.trade_pnl 主口径
    同一实现，窗口/风险位常量单一来源 execution_layer.MF_BUY。不成交返回 None。"""
    b = _MF_BUY[grp]
    if o is None or c is None or not (b["win_min"] <= o <= b["win_max"]):
        return None
    if grp == "nlu" and (h is None or h < max(o, 0.0)):
        return None                      # 限价挂 max(开盘,昨收)，当日未触及=未成交
    entry = o if grp == "lu" else max(o, 0.0)
    stop = b["low_min"] if grp == "lu" else b["risk_low"]
    hit = l is not None and ((l < stop) if grp == "lu" else (l <= stop))
    exit_pct = stop if hit else c
    return round(((1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1) * 100, 2)


def _val_exec_stats(rows):
    """执行口径度量（C8 度量层，2026-09-10 全窗口实证后新增）：
    放弃/可成交/实时均益/MFE/MAE/止损质量——条件期望与可执行收益的差距留痕。"""
    out = {}
    for grp in ("lu", "nlu"):
        b = _MF_BUY[grp]
        stop = b["low_min"] if grp == "lu" else b["risk_low"]
        rs = [p for p in rows if p.get("group") == grp]
        oo = [p["open"] for p in rs if p.get("open") is not None]
        exec_rows = []
        for p in rs:
            pnl = _exec_pnl_realtime(grp, p.get("open"), p.get("high"),
                                     p.get("low"), p.get("close"))
            if pnl is not None:
                exec_rows.append((p, pnl))
        pnls = [pnl for _p, pnl in exec_rows]
        stopped = [(p, pnl) for p, pnl in exec_rows
                   if p.get("low") is not None
                   and ((p["low"] < stop) if grp == "lu" else (p["low"] <= stop))]
        whipsaw = [p for p, _pnl in stopped
                   if p.get("close") is not None
                   and p["close"] > (p["open"] if grp == "lu" else max(p["open"], 0.0))]
        saved = []
        for p, pnl in stopped:
            ent = p["open"] if grp == "lu" else max(p["open"], 0.0)
            if p.get("close") is not None and p["close"] < ent:
                saved.append(round(((1 + stop / 100.0) / (1 + ent / 100.0) - 1) * 100 - pnl, 2))

        def _avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        out[grp] = {
            "n": len(rs),
            "in_window": (round(100.0 * sum(1 for x in oo
                                            if b["win_min"] <= x <= b["win_max"]) / len(oo), 1)
                          if oo else None),
            "n_exec": len(exec_rows),
            "pnl_avg": (round(sum(pnls) / len(pnls), 2) if pnls else None),
            "mfe_avg": _avg([p["high"] - (p["open"] if grp == "lu" else max(p["open"], 0.0))
                             for p, _pnl in exec_rows if p.get("high") is not None]),
            "mae_avg": _avg([p["low"] - (p["open"] if grp == "lu" else max(p["open"], 0.0))
                             for p, _pnl in exec_rows if p.get("low") is not None]),
            "stop_hit": len(stopped),
            "stop_whipsaw": len(whipsaw),
            "stop_saved_avg": (round(sum(saved) / len(saved), 2) if saved else None),
        }
    return out


def _load_track():
    try:
        with open(POOL_TRACK, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("validations"), dict):
            return d
    except Exception:  # noqa: BLE001 - 无/坏文件=空记录起步
        pass
    return {"validations": {}}


def _save_track(track):
    fsutil.save_json_atomic(POOL_TRACK, track)


def record_validation(date8, val):
    """验证记录按验证日覆盖写（回填/重跑幂等）。"""
    track = _load_track()
    track["validations"][str(date8)] = val
    # 只保留最近 180 个验证日，防无限膨胀
    keys = sorted(track["validations"])[-180:]
    track["validations"] = {k: track["validations"][k] for k in keys}
    _save_track(track)


def _optimizer_state(date8):
    """滚动窗口（仅 C7 口径样本）→ 有界旋钮。纯函数：同输入必同输出。"""
    track = _load_track()
    iso_day = _dt.datetime.strptime(_date_iso(date8), "%Y-%m-%d")
    cutoff = iso_day - _dt.timedelta(days=_OPT_WINDOW_DAYS)
    rows = []
    for key in sorted(track.get("validations") or {}):
        try:
            kd = _dt.datetime.strptime(key, "%Y%m%d")
        except ValueError:
            continue
        if kd < cutoff or kd > iso_day:
            continue
        for p in ((track["validations"][key] or {}).get("picks") or []):
            # 只学 C7 口径样本（旧线分桶语义不同：如 concept_cold 阈值已变）；
            # 旧线 picks 照常验证留痕，与 M 线"只学本线"纪律一致
            if p.get("rules") != "c7" or p.get("close") is None:
                continue
            rows.append(p)
    state = {"min_score": {}, "penalties": {}, "samples": {}, "bucket_stats": {}}
    for grp in ("lu", "nlu"):
        g = [p for p in rows if p.get("group") == grp]
        lo, hi = _OPT_MIN_SCORE[grp]
        cr = [p["close"] for p in g]
        ms = float(_OPT_BASELINE_MINSCORE[grp])   # 无证据时保持 C7 固化基线，不自动放宽
        if len(cr) >= _OPT_MINSCORE_MIN_N and sum(cr) / len(cr) < 0:
            # 近期均值为负 → 抬门槛过滤边缘分（幅度与负程度挂钩，封顶 hi）
            ms = min(hi, ms + min(10.0, math.ceil(abs(sum(cr) / len(cr)) * 2)))
        state["min_score"][grp] = ms
        state["samples"][grp] = len(g)
        buckets = {}
        for p in g:
            for bk in (p.get("buckets") or []):
                buckets.setdefault(bk, []).append(p["close"])
        pens, bstat = {}, {}
        for bk, xs in sorted(buckets.items()):
            avg = sum(xs) / len(xs)
            bstat[bk] = {"n": len(xs), "avg": round(avg, 2)}
            if len(xs) >= _OPT_BUCKET_MIN_N:
                if avg <= -1.0:
                    pens[bk] = 4
                elif avg <= -0.4:
                    pens[bk] = 2
        state["penalties"][grp] = pens
        state["bucket_stats"][grp] = bstat
    return state


# ---- 样本外追踪与到期复审（C7 固化后；2026-09-11 由 C_Final 线切换）----
# C7 在 2026-09-09 固化（strategy-iter us_round5_C7_aligned，窗口 01-05..09-08）；
# 此后每日验证闭环里 rules="c7" 的记录就是真正的样本外。观察不能只挂不办：
# 每满 20 个样本外验证日复审一次，胜率/均次跌破明确阈值必须给出结论（写死，不漂移）。
_OOS_BASELINE = {"win_rate": 62.09, "avg_close": 2.497}   # C7 对齐run stats.json overall（n=422 条件口径）
_OOS_REVIEW_EVERY = 20      # 复审周期（样本外验证日数）
_OOS_WARN_WIN_DROP = 8.0    # 胜率较基线回落 ≥8pct → 警告（轮间稳定阈值 3pct 的两倍余量）
_OOS_WARN_MEAN = 1.0        # 或均次 <1.0%（基线约一半）
_OOS_CRIT_WIN_DROP = 12.0   # 胜率回落 ≥12pct 或均次 <0 → 严重衰减，建议重开迭代

_OOS_VERDICT_TEXT = {
    "ok": "复审通过：样本外与固化窗口基线无实质差异，C7 继续运行",
    "warn": "警告：样本外表现回落——人工复核归因分布，规则暂不动",
    "severe": "严重衰减：按《自动选股与策略自迭代系统.md》重开完整迭代",
}


def _oos_verdict(win_rate, avg_close, n):
    """累积样本外统计 → 判定（纯函数）。n<=0 返回 empty。"""
    if n <= 0:
        return "empty"
    drop = _OOS_BASELINE["win_rate"] - win_rate
    if drop >= _OOS_CRIT_WIN_DROP or avg_close < 0:
        return "severe"
    if drop >= _OOS_WARN_WIN_DROP or avg_close < _OOS_WARN_MEAN:
        return "warn"
    return "ok"


def _c7_rows(track):
    """全部 C7 口径验证样本（close 非空）。样本外追踪与观察项共用。"""
    rows = []
    for key in sorted(track.get("validations") or {}):
        v = (track.get("validations") or {}).get(key) or {}
        if v.get("rules") != "c7":
            continue
        rows.extend(p for p in (v.get("picks") or []) if p.get("close") is not None)
    return rows


def _oos_state(track):
    """pool_track → C7 样本外追踪（纯函数）。

    只统计 rules=="c7" 的验证记录：逐日聚合 + 累积序列 + 整体统计与当前判定。
    """
    vals = track.get("validations") or {}
    per_day, rows_all = [], []
    for key in sorted(vals):
        v = vals[key] or {}
        if v.get("rules") != "c7":
            continue
        picks = [p for p in (v.get("picks") or []) if p.get("close") is not None]
        rows_all.extend(picks)
        cr = [p["close"] for p in picks]
        row = {"date": key, "valid": len(picks),
               "win_rate": (round(100.0 * sum(1 for x in cr if x > 0) / len(cr), 1)
                            if cr else None),
               "avg_close": round(sum(cr) / len(cr), 2) if cr else None}
        row["_wins"] = sum(1 for x in cr if x > 0)
        row["_sum"] = sum(cr)
        per_day.append(row)
    cr = [p["close"] for p in rows_all]
    n = len(cr)
    win_rate = round(100.0 * sum(1 for x in cr if x > 0) / n, 2) if n else None
    avg_close = round(sum(cr) / n, 3) if n else None
    trig = [p for p in rows_all if p.get("buy_triggered")]
    sc = [p for p in rows_all if p.get("strong_concept") is not None]
    cum, rn, rs, rw = [], 0, 0.0, 0
    for d in per_day:
        rn += d["valid"]
        rs += d["_sum"]
        rw += d["_wins"]
        cum.append({"date": d["date"], "n": rn,
                    "win_rate": round(100.0 * rw / rn, 2) if rn else None,
                    "avg_close": round(rs / rn, 3) if rn else None})
    for d in per_day:
        d.pop("_wins")
        d.pop("_sum")   # 内部累加字段不下发
    days = len(per_day)
    return {"days": days, "n": n,
            "since": per_day[0]["date"] if per_day else None,
            "win_rate": win_rate, "avg_close": avg_close,
            "trigger_rate": (round(100.0 * len(trig) / n, 1) if n else None),
            "strong_concept_rate": (round(100.0 * sum(1 for p in sc if p.get("strong_concept")) / len(sc), 1)
                                    if sc else None),
            "baseline": dict(_OOS_BASELINE),
            "verdict": _oos_verdict(win_rate or 0.0, avg_close or 0.0, n),
            "review_every": _OOS_REVIEW_EVERY,
            "next_review_in": (_OOS_REVIEW_EVERY - (days % _OOS_REVIEW_EVERY)) if days else None,
            "per_day": per_day[-10:], "cum": cum}


# ---- 观察项到期动作（rounds_log C 线终止决定遗留观察项③④）----
# 观察项不能无限期挂着：累积样本到线必须二选一——升级（给下轮迭代明确建议）或销项。
_OBS_CONCEPT_COLD_N = 30        # 概念温吞桶（concept_cold：概念内涨停<3 家仍入选）到期线
_OBS_CONCEPT_COLD_AVG = -0.4    # 升级阈值（与优化器分桶惩罚同阈：均次 ≤-0.4%）
_OBS_STRONG_CONCEPT_N = 40      # strong_concept 率观察到期线
_OBS_STRONG_CONCEPT_MIN = 30.0  # 强于概念率 <30%（历史 ~41%，调参区间 51.6%）→ 复审概念因子


def _observation_state(rows):
    """C_Final 全量样本 → 两个遗留观察项的到期状态（纯函数）。

    - concept_cold（遗留③「概念内涨停 ≤2 家残余负桶样本不足，继续观察」）：
      到线后均次 ≤-0.4% → escalate（建议下轮迭代把概念家数 <3 从降权升为硬排除）；
      否则 closed（负桶未证实，销项）。
    - strong_concept（遗留④「跑赢自身概念指数难度高，仅作监控不作门槛」）：
      到线后率 <30% → escalate（概念因子有效性存疑，提示复审）；否则 closed
      （确认结论：维持仅监控，不作门槛）。
    """
    cc = [p["close"] for p in rows if "concept_cold" in (p.get("buckets") or [])]
    sc = [p for p in rows if p.get("strong_concept") is not None]
    th_cc = {"n": _OBS_CONCEPT_COLD_N, "avg": _OBS_CONCEPT_COLD_AVG}
    if len(cc) >= _OBS_CONCEPT_COLD_N:
        avg = sum(cc) / len(cc)
        out_cc = {"n": len(cc), "avg_close": round(avg, 2),
                  "status": "escalate" if avg <= _OBS_CONCEPT_COLD_AVG else "closed",
                  "thresholds": th_cc}
    else:
        out_cc = {"n": len(cc), "status": "observing", "thresholds": th_cc}
    th_sc = {"n": _OBS_STRONG_CONCEPT_N, "min_rate": _OBS_STRONG_CONCEPT_MIN}
    if len(sc) >= _OBS_STRONG_CONCEPT_N:
        rate = 100.0 * sum(1 for p in sc if p.get("strong_concept")) / len(sc)
        out_sc = {"n": len(sc), "rate": round(rate, 1),
                  "status": "escalate" if rate < _OBS_STRONG_CONCEPT_MIN else "closed",
                  "thresholds": th_sc}
    else:
        out_sc = {"n": len(sc), "status": "observing", "thresholds": th_sc}
    return {"concept_cold": out_cc, "strong_concept": out_sc}


def update_optimizer(date8, persist=True):
    """重算优化器状态并留痕；返回随快照下发的说明 payload。

    persist=False（retrofill 回填路径）：只算 payload 供快照展示，不写
    pool_opt.json——回填不得推动 45 日优化器与样本外复审状态（防污染纪律）。"""
    state = _optimizer_state(date8)
    opt = _load_opt()
    prev_log = opt.get("log") or []
    last = prev_log[-1] if prev_log else None
    # 比对基准：上一验证日状态；首次运行与 C7 固化基线比对（默认口径→首个非默认
    # 状态也算一次调整，否则首个负期望窗口永远不会被记录）
    ref = last if last else {"min_score": {"lu": 70.0, "nlu": 70.0},
                             "penalties": {"lu": {}, "nlu": {}}}
    changed = []
    if (ref.get("min_score") or {}) != state["min_score"]:
        changed.append("入选门槛 {}→{}".format(ref.get("min_score"), state["min_score"]))
    for grp in ("lu", "nlu"):
        a = (ref.get("penalties") or {}).get(grp) or {}
        bb = state["penalties"][grp]
        for k in sorted(set(a) | set(bb)):
            if a.get(k) != bb.get(k):
                changed.append(f"{'涨停组' if grp == 'lu' else '低吸组'}.{k} "
                               f"惩罚 {a.get(k) or 0}→{bb.get(k) or 0}")
    log = [e for e in prev_log if e.get("date") != str(date8)]
    log.append({"date": str(date8), "min_score": state["min_score"],
                "penalties": state["penalties"], "samples": state["samples"],
                "changed": changed})
    log = log[-90:]
    n_adj = len([e for e in log if e.get("changed")])
    ver = f"C7+o{n_adj}" if n_adj else "C7"
    enough = state["samples"]["lu"] + state["samples"]["nlu"] >= _OPT_MINSCORE_MIN_N
    # 样本外追踪 + 到期复审 + 观察项到期（每日验证闭环的「验收」侧，与旋钮同处留痕）
    track_all = _load_track()
    oos = _oos_state(track_all)
    obs = _observation_state(_c7_rows(track_all))
    reviews = list(opt.get("oos_reviews") or [])
    review = None
    if oos["days"] > 0 and oos["days"] % _OOS_REVIEW_EVERY == 0 \
            and not any(r.get("date") == str(date8) for r in reviews):
        review = {"date": str(date8), "n_days": oos["days"], "n": oos["n"],
                  "win_rate": oos["win_rate"], "avg_close": oos["avg_close"],
                  "verdict": oos["verdict"],
                  "note": _OOS_VERDICT_TEXT.get(oos["verdict"], oos["verdict"])}
        reviews.append(review)
        reviews = reviews[-20:]
    oos["reviews"] = reviews[-5:]
    oos["review_today"] = review
    out = {"version": ver, "updated": str(date8),
           "min_score": state["min_score"], "penalties": state["penalties"],
           "bucket_stats": state["bucket_stats"], "samples": state["samples"],
           "window_days": _OPT_WINDOW_DAYS, "changes": changed,
           "oos": oos, "observations": obs,
           "note": ("今日调整： " + "；".join(changed)) if changed else
                   ("窗口内无需调整（涨停组 n=%d / 低吸组 n=%d）"
                    % (state["samples"]["lu"], state["samples"]["nlu"])) if enough else
                   ("窗口样本积累中（涨停组 n=%d / 低吸组 n=%d），按 C7 固化基线口径"
                    % (state["samples"]["lu"], state["samples"]["nlu"]))}
    if persist:
        fsutil.save_json_atomic(POOL_OPT, {
            "version": ver, "updated": str(date8),
            "min_score": state["min_score"], "penalties": state["penalties"],
            "oos_reviews": reviews,
            "log": log})
    return out
