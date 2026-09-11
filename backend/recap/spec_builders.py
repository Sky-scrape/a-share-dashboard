# -*- coding: utf-8 -*-
"""快照构建器：themes/ladder/deviation/fate/cycle/anomalies 六个复盘视图的纯计算（输入快照行，输出 payload）。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import ht  # noqa: E402
from modules import cget  # noqa: E402
from spec_duckdb import CACHE_DIR, day_pcts, scan_deviation  # noqa: E402
from spec_rules import (INDEX_BENCH, NEAR_D10, NEAR_D30, NORMAL_D3,  # noqa: E402
                        SEVERE_D10, SEVERE_D30,
                        _date_iso, _first_theme, _round2, board_class, thscode_of)
from spec_series import index_gain, index_series  # noqa: E402


# ---------------------------------------------------------------- 题材与梯队

def build_themes(zt_rows):
    """按涨停原因首段聚类；核心 = 连板最高（封板时间最早 → 封单最大 平手裁决）。

    返回 {groups, summary}：summary 给题材分散度一个诚实的量化描述
    （文案：大题材分支多各有核心；独苗/分散市场没有合力）。
    """
    groups = {}
    for r in zt_rows:
        groups.setdefault(_first_theme(r.get("涨停原因")), []).append(r)
    out = []
    for theme, rows in groups.items():
        rows = sorted(rows, key=lambda r: (
            -(int(r.get("连板数") or 1)),
            str(r.get("首次封板时间") or "99:99"),
            -(float(r.get("封板资金") or 0)),
        ))
        core = rows[0]
        back = len(rows) - 1
        out.append({
            "题材": theme, "家数": len(rows),
            "高度": max(int(r.get("连板数") or 1) for r in rows),
            "后排": back,
            "持续性": ("独苗题材·无后排" if back == 0 else
                       "后排充足" if back >= 4 else "有后排"),
            "核心": {
                "代码": str(cget(core, "code") or ""),
                "名称": cget(core, "name"),
                "连板": int(core.get("连板数") or 1),
                "封板时间": core.get("首次封板时间"),
                "封板资金": core.get("封板资金"),
                "涨停原因": core.get("涨停原因"),
                "is_st": bool(core.get("is_st")),
            },
            "成员": [{"代码": str(cget(r, "code") or ""),
                       "名称": cget(r, "name"),
                       "连板": int(r.get("连板数") or 1)} for r in rows],
        })
    out.sort(key=lambda t: (-t["高度"], -t["家数"], t["题材"]))
    total = len(zt_rows)
    top3 = sum(t["家数"] for t in out[:3])
    biggest = out[0] if out else None
    if not total:
        verdict = "当日无涨停"
    elif biggest and biggest["家数"] >= 8:
        verdict = "题材效应集中·有主线雏形"
    elif biggest and biggest["家数"] >= 3:
        verdict = "题材偏分散·局部有合力"
    else:
        verdict = "极度分散·以独立逻辑为主"
    summary = {
        "题材数": len(out), "涨停总数": total,
        "最大题材": (biggest["题材"] if biggest else None),
        "最大题材家数": (biggest["家数"] if biggest else 0),
        "前三题材覆盖": top3,
        "前三占比": round(top3 / total * 100) if total else None,
        "定性": verdict,
    }
    return {"groups": out, "summary": summary}


def build_ladder(zt_rows):
    by_lb = {}
    for r in zt_rows:
        lb = int(r.get("连板数") or 1)
        by_lb.setdefault(lb, []).append({
            "代码": str(cget(r, "code") or ""), "名称": cget(r, "name"),
            "原因": _first_theme(r.get("涨停原因")),
        })
    rows = [{"板数": k, "家数": len(v), "个股": v}
            for k, v in sorted(by_lb.items(), reverse=True)]
    return {"rows": rows, "max_lb": (rows[0]["板数"] if rows else 0)}


# ---------------------------------------------------------------- 偏离值雷达

def _dev_status(board, dev3, dev10, dev30):
    if board == "bj":
        return "bj"
    if ((dev10 is not None and dev10 >= SEVERE_D10)
            or (dev30 is not None and dev30 >= SEVERE_D30)):
        return "severe"
    if ((dev10 is not None and dev10 >= NEAR_D10)
            or (dev30 is not None and dev30 >= NEAR_D30)):
        return "near"
    th = NORMAL_D3.get(board, 20.0)
    if dev3 is not None and abs(dev3) >= th:
        return "normal"
    return "watch"


def build_deviation(date8, zt_rows, zb_rows):
    """偏离值雷达：全市场扫描 + 池内个股强制入选。失败抛异常由上层降级。"""
    pool = {}
    for r in (zt_rows or []):
        pool[str(cget(r, "code") or "")] = ("zt", r)
    for r in (zb_rows or []):
        c = str(cget(r, "code") or "")
        if c and c not in pool:
            pool[c] = ("zb", r)
    extra = [thscode_of(c) for c in pool]

    rows = scan_deviation(date8, extra_thscodes=extra)
    iso = _date_iso(date8)
    gains = {}
    for mkt in ("SH", "SZ"):
        series = index_series(mkt, date8)
        gains[mkt] = {
            "code": INDEX_BENCH[mkt][0], "name": INDEX_BENCH[mkt][1],
            "g3": index_gain(series, iso, 3),
            "g10": index_gain(series, iso, 10),
            "g30": index_gain(series, iso, 30),
        }

    name_map = None   # 懒加载（仅池外个股需要）
    out = []
    for row in rows:
        thscode = str(row.get("thscode") or "")
        code6 = thscode.split(".")[0]
        mkt = thscode.split(".")[-1]
        board = board_class(code6)
        g = gains.get(mkt)
        if g is None:      # 北交所无基准：保留行但不判定
            dev3 = dev10 = dev30 = None
        else:
            dev3 = (None if row.get("r3") is None or g["g3"] is None
                    else row["r3"] - g["g3"])
            dev10 = (None if row.get("r10") is None or g["g10"] is None
                     else row["r10"] - g["g10"])
            dev30 = (None if row.get("r30") is None or g["g30"] is None
                     else row["r30"] - g["g30"])
        space10 = None if dev10 is None else max(SEVERE_D10 - dev10, 0)
        space30 = None if dev30 is None else max(SEVERE_D30 - dev30, 0)
        spaces = [s for s in (space10, space30) if s is not None]
        status = _dev_status(board, dev3, dev10, dev30)

        hit = pool.get(code6)
        name, lb, theme, is_st = None, None, None, False
        if hit:
            src, pr = hit
            name = cget(pr, "name")
            lb = int(pr.get("连板数") or 0) if src == "zt" else None
            theme = _first_theme(pr.get("涨停原因"))
            is_st = bool(pr.get("is_st"))
        if name is None:
            if name_map is None:
                name_map = ht.symbol_names(cache_dir=CACHE_DIR)
            name = name_map.get(code6) or code6
        if not name:
            name = code6

        out.append({
            "代码": code6, "名称": name, "thscode": thscode, "board": board,
            "今涨": row.get("r1"), "连板": lb, "题材": theme,
            "in_zt": code6 in pool and pool[code6][0] == "zt",
            "in_zb": code6 in pool and pool[code6][0] == "zb",
            "is_st": is_st or "ST" in str(name).upper(),
            "r3": row.get("r3"), "r10": row.get("r10"), "r30": row.get("r30"),
            "dev3": _round2(dev3), "dev10": _round2(dev10), "dev30": _round2(dev30),
            "space10": _round2(space10), "space30": _round2(space30),
            "空间": _round2(min(spaces)) if spaces else None,
            "status": status,
        })

    # 排序：严重 > 逼近 > 普通 > 观察 > 北交所（不在判定口径内，沉底）；
    # 同状态内按「最接近哪条严重线」降序。bj 与 watch 分属不同 rank，
    # 不能用单一分值混排（量纲不同会把北交所顶到最前）。
    _STATUS_RANK = {"severe": 0, "near": 1, "normal": 2, "watch": 3, "bj": 4}

    def _sev(x):
        rank = _STATUS_RANK.get(x["status"], 5)
        if x["status"] == "bj":
            score = 0.0
        else:
            score = max(
                (x["dev10"] if x["dev10"] is not None else -999) / SEVERE_D10,
                (x["dev30"] if x["dev30"] is not None else -999) / SEVERE_D30)
        return (rank, -score)

    out.sort(key=_sev)
    return {"benchmark": gains, "rows": out[:80], "total": len(out)}


# ---------------------------------------------------------------- 高位承接

_FATE_ORDER = {"连板晋级": 0, "炸板": 1, "断板收涨": 2, "断板走弱": 3,
               "断板大跌": 4, "跌停": 5, "未知": 6}


def build_fate(date8, prev_zt_rows, bundles):
    """昨日连板≥2 个股今日结局。DuckDB 失败仅影响今日涨跌幅列。"""
    if not prev_zt_rows:
        return {"rows": [], "stats": [], "note": "昨日快照无涨停池，无法计算承接"}
    zt_today = {str(cget(r, "code") or "") for r in (bundles.get("zt") or [])}
    zb_today = {str(cget(r, "code") or "") for r in (bundles.get("zb") or [])}
    dt_today = {str(cget(r, "code") or "") for r in (bundles.get("dt") or [])}

    cands = [r for r in prev_zt_rows if int(r.get("连板数") or 1) >= 2]
    if not cands:
        return {"rows": [], "stats": [], "note": "昨日无 2 连板以上个股"}

    pcts = {}
    try:
        pcts = day_pcts(date8, [thscode_of(str(cget(r, "code") or ""))
                                for r in cands])
        pcts = {k.split(".")[0]: v for k, v in pcts.items()}
    except Exception as e:  # noqa: BLE001 - 涨跌幅缺失不阻断结局分类
        note = f"（DuckDB 涨跌幅不可用: {type(e).__name__}）"
    else:
        note = ""

    rows = []
    for r in cands:
        code = str(cget(r, "code") or "")
        pct = pcts.get(code)
        if code in zt_today:
            outcome = "连板晋级"
        elif code in dt_today:
            outcome = "跌停"
        elif code in zb_today:
            outcome = "炸板"
        elif pct is None:
            outcome = "未知"
        elif pct <= -5:
            outcome = "断板大跌"
        elif pct < 0:
            outcome = "断板走弱"
        else:
            outcome = "断板收涨"
        rows.append({
            "代码": code, "名称": cget(r, "name"),
            "昨连板": int(r.get("连板数") or 1),
            "题材": _first_theme(r.get("涨停原因")),
            "今涨": pct, "结局": outcome,
        })
    rows.sort(key=lambda x: (x["昨连板"], -_FATE_ORDER.get(x["结局"], 9)),
              reverse=True)

    stats = []
    by_lb = {}
    for x in rows:
        by_lb.setdefault(x["昨连板"], []).append(x)
    for lb in sorted(by_lb, reverse=True):
        xs = by_lb[lb]
        pcts_ok = [x["今涨"] for x in xs if x["今涨"] is not None]
        up_n = sum(1 for x in xs
                   if x["结局"] == "连板晋级" or (x["今涨"] or -99) > 0)
        stats.append({
            "板数": lb, "家数": len(xs),
            "晋级": sum(1 for x in xs if x["结局"] == "连板晋级"),
            "红盘率": round(up_n / len(xs) * 100) if xs else None,
            "均涨": _round2(sum(pcts_ok) / len(pcts_ok)) if pcts_ok else None,
            "大跌": sum(1 for x in xs if x["结局"] in ("断板大跌", "跌停")),
        })
    return {"rows": rows, "stats": stats, "note": note or None}


# ---------------------------------------------------------------- 情绪周期

def _avg(key, rs):
    vals = [r[key] for r in rs if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def build_cycle(date8, sent_rows):
    """启动-发酵-高潮-震荡-退潮 的规则化定位（近3日 vs 前3日，机械推断）。"""
    iso = _date_iso(date8)
    rows = [r for r in sent_rows if (r["date"] or "") <= iso]
    if len(rows) < 8:
        return {"phase": "数据不足", "reasons": ["情绪序列不足 8 个交易日"],
                "series": rows[-30:], "indicators": {}}
    cur = rows[-1]
    caveat = None if cur["date"] == iso else f"注：{iso} 无情绪数据，以 {cur['date']} 为最新"
    p3 = rows[-6:-3]

    zt_now, zt_p3 = cur["zt"], _avg("zt", p3)
    lb_now, lb_p3 = cur["max_lb"], _avg("max_lb", p3)
    pr_now, pr_p3 = cur["promo_rate"], _avg("promo_rate", p3)
    lb_win = [r["max_lb"] for r in rows[-30:] if r["max_lb"] is not None]

    reasons = []
    if zt_now is not None and zt_p3 is not None:
        reasons.append(f"涨停 {int(zt_now)} 家（前3日均 {zt_p3:.0f}）")
    if lb_now is not None and lb_p3 is not None:
        reasons.append(f"最高 {int(lb_now)} 板（前3日均 {lb_p3:.1f}）")
    if pr_now is not None and pr_p3 is not None:
        reasons.append(f"晋级率 {pr_now*100:.0f}%（前3日均 {pr_p3*100:.0f}%）")

    phase = "震荡"
    if zt_now is not None and lb_now is not None and zt_now >= 80 and lb_now >= 6:
        phase = "高潮"
    elif zt_now is not None and zt_now <= 25:
        phase = "冰点"
    elif (lb_now is not None and lb_p3 is not None and lb_now < lb_p3 - 0.7) or \
            (pr_now is not None and pr_p3 is not None
             and pr_now < pr_p3 - 0.12
             and zt_now is not None and zt_p3 is not None and zt_now < zt_p3):
        phase = "退潮"
    elif (lb_now is not None and lb_p3 is not None and lb_now > lb_p3 + 0.5
          and zt_now is not None and zt_p3 is not None and zt_now >= zt_p3):
        phase = "发酵"
    if phase == "冰点" and zt_now is not None and zt_p3 is not None \
            and zt_now > zt_p3 + 5:
        phase = "启动"
    if lb_win:
        reasons.append(f"30日高度区间 {int(min(lb_win))}–{int(max(lb_win))} 板")
    if caveat:
        reasons.append(caveat)

    return {
        "phase": phase, "reasons": reasons,
        "indicators": {
            "zt": zt_now, "zt_prev3": zt_p3, "max_lb": lb_now,
            "max_lb_prev3": lb_p3, "promo": pr_now, "promo_prev3": pr_p3,
            "sentiment": cur["index"],
        },
        "series": [{"date": r["date"], "zt": r["zt"], "max_lb": r["max_lb"],
                    "promo": r["promo_rate"], "idx": r["index"]}
                   for r in rows[-30:]],
    }


# ---------------------------------------------------------------- 异动事件

def build_anomalies(historical):
    if historical:
        return {"available": False,
                "note": "异动榜单为当日接口（today-only），历史日期不可回补"}
    d = ht.ht("special", "anomaly-list", timeout=90)
    rows = []
    for r in (d.get("item") or []):
        content = str(r.get("analysis_content") or "")
        summary = content.split("（免责声明")[0].strip()
        if len(summary) > 220:
            summary = summary[:220] + "…"
        thscode = str(r.get("thscode") or "")
        rows.append({
            "代码": thscode.split(".")[0], "名称": r.get("stock_name"),
            "标签": r.get("tag_name"),
            "关键词": r.get("keyword_list") or [],
            "摘要": summary,
        })
    return {"available": True, "rows": rows[:40], "total": len(rows)}
