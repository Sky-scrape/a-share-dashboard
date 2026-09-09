# -*- coding: utf-8 -*-
"""派生层：从原始快照/分时数据计算研究面板，输出到 data/*/panel/。

职责（原 server.py 内嵌逻辑全部迁到这里，HTTP 层只读文件）：
  recap/panel/sentiment.csv    多因子市场情绪指数序列（带 weights_version）
  recap/panel/promotion.csv    晋级率（按日期相邻现算，不再依赖快照内嵌 prev 的抓取顺序）
  rotation/panel/stats.json    轮动速度 / Top10 / 持续强势 / 新晋 / 5日领涨（API 兼容旧形状）
  rotation/panel/matrix.json   板块×日 强度矩阵 + 日内形态统计（分时数据沉淀）

用法:
  python backend/derive.py            # 仅当源数据比面板新时才重算
  python backend/derive.py --force    # 强制全量重算
"""
import argparse
from pathlib import Path
import csv
import io
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))

import snapio  # noqa: E402
from modules import cget, fget  # noqa: E402

RECAP_DIR = os.path.join(ROOT, "data", "recap")
ROT_DAILY = os.path.join(ROOT, "data", "rotation", "daily")
PANEL_RECAP = os.path.join(RECAP_DIR, "panel")
PANEL_ROT = os.path.join(ROOT, "data", "rotation", "panel")

# ---- 情绪指数权重（v1 与原 server.py 完全一致，保证历史曲线连续）----
WEIGHTS_VERSION = 1
W = {
    "zt_full": 120,   # 涨停数满分线
    "w_zt": 25,
    "lb_full": 8,     # 最高连板满分线
    "w_lb": 20,
    "w_promo": 20,    # 晋级率
    "w_up": 20,       # 上涨占比
    "dt_full": 50,    # 跌停数反向满分线
    "w_dt": 15,
}


def _mtime_newest(paths):
    m = 0.0
    for p in paths:
        try:
            m = max(m, os.path.getmtime(p))
        except OSError:
            pass
    return m


def _out_mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _is_stale(out_path, src_max):
    return _out_mtime(out_path) < src_max


# ============================ recap 侧 ============================

def _zt_codes_of(snap):
    rows = ((snap.get("modules") or {}).get("limit_up_pool") or {}).get("data") or []
    return {str(cget(r, "code") or "") for r in rows if cget(r, "code")}


def _sent_day_feat(d):
    """单日情绪特征（涨停代码集/跌停数/最高连板/涨跌家数/炸板），按 (日期, mtime) 进程内缓存。"""
    fp = snapio.snap_path(RECAP_DIR, d)
    try:
        m = os.path.getmtime(fp)
    except OSError:
        return None
    ent = _sent_day_cache.get(d)
    if ent and ent["mtime"] == m:
        return ent["feat"]
    snap = snapio.load(d, RECAP_DIR)
    if snap is None:
        return None
    mod = snap.get("modules") or {}
    zt_rows = (mod.get("limit_up_pool") or {}).get("data") or []
    max_lb = 0
    for r in zt_rows:
        try:
            max_lb = max(max_lb, int(fget(r, "连板数", "连续涨停天数") or 0))
        except (TypeError, ValueError):
            pass
    bd = (mod.get("breadth") or {}).get("data") or {}
    feat = {
        "zt": _zt_codes_of(snap),
        "dt": len((mod.get("limit_down_pool") or {}).get("data") or []),
        "max_lb": max_lb,
        "up": bd.get("上涨") or 0,
        "down": bd.get("下跌") or 0,
        "zhaban": bd.get("炸板家数") or 0,
    }
    _sent_day_cache[d] = {"mtime": m, "feat": feat}
    return feat


_sent_day_cache = {}   # date -> {"mtime": float, "feat": {...}}


def compute_sentiment():
    """返回 (sentiment_rows, promotion_rows)，按日期升序。

    单日特征按 (date, mtime) 缓存，增量刷新只解析新增/变更的快照文件；
    输出与逐日全量解析版本完全一致。
    """
    dates = sorted(snapio.list_dates(RECAP_DIR))
    feats = []
    for d in dates:
        f = _sent_day_feat(d)
        if f is not None:
            feats.append((d, f))
    sent, promo = [], []
    prev = None
    for d, f in feats:
        zt = f["zt"]
        dt = f["dt"]
        max_lb = f["max_lb"]
        up_ratio = round(f["up"] / (f["up"] + f["down"]), 3) if (f["up"] + f["down"]) else None
        # 晋级率：昨日涨停 ∩ 今日涨停 / 昨日涨停（按日期相邻现算）
        p_codes = prev["zt"] if prev else set()
        promo_rate = round(len(zt & p_codes) / len(p_codes), 3) if p_codes else None
        if p_codes:
            promo.append({
                "date": d, "prev_date": prev["date"], "prev_zt": len(p_codes),
                "promo_zt": len(zt & p_codes), "promo_rate": promo_rate,
            })
        # 合成指数（0-100）
        if promo_rate is None and up_ratio is None:
            idx, label = None, ""
        else:
            zs = min(len(zt) / W["zt_full"], 1) * W["w_zt"]
            ls = min(max_lb / W["lb_full"], 1) * W["w_lb"]
            rs = (promo_rate or 0) * W["w_promo"]
            us = (up_ratio if up_ratio is not None else 0.5) * W["w_up"]
            ds = (1 - min(dt / W["dt_full"], 1)) * W["w_dt"]
            idx = round(min(zs + rs + ls + us + ds, 100), 1)
            label = ("亢奋" if idx >= 70 else "发酵" if idx >= 55 else
                     "震荡" if idx >= 40 else "冰点")
        sent.append({
            "version": WEIGHTS_VERSION, "date": d, "index": idx, "label": label,
            "zt": len(zt), "dt": dt, "max_lb": max_lb,
            "promo_rate": promo_rate, "up_ratio": up_ratio, "zhaban": f["zhaban"],
        })
        prev = {"date": d, "zt": zt}
    return sent, promo


def derive_recap(force=False):
    os.makedirs(PANEL_RECAP, exist_ok=True)
    sent_out = os.path.join(PANEL_RECAP, "sentiment.csv")
    promo_out = os.path.join(PANEL_RECAP, "promotion.csv")
    dates = snapio.list_dates(RECAP_DIR)
    src_max = _mtime_newest([snapio.snap_path(RECAP_DIR, d) for d in dates])
    if not force and not _is_stale(sent_out, src_max):
        return False
    sent, promo = compute_sentiment()
    def _csv_text(fieldnames, rows):
        buf = io.StringIO()
        wcsv = csv.DictWriter(buf, fieldnames=fieldnames)
        wcsv.writeheader()
        wcsv.writerows(rows)
        return buf.getvalue()

    Path(sent_out).write_text(
        _csv_text(["version", "date", "index", "label", "zt",
                   "dt", "max_lb", "promo_rate", "up_ratio", "zhaban"], sent),
        encoding="utf-8-sig")
    Path(promo_out).write_text(
        _csv_text(["date", "prev_date", "prev_zt", "promo_zt", "promo_rate"], promo),
        encoding="utf-8-sig")
    print(f"derive: sentiment {len(sent)} 天 · promotion {len(promo)} 天 -> {PANEL_RECAP}")
    return True


# ============================ rotation 侧 ============================

def _rot_dates():
    if not os.path.isdir(ROT_DAILY):
        return []
    return sorted(f[:-5] for f in os.listdir(ROT_DAILY)
                  if f.endswith(".json") and len(f) == 15)


def _day_tops(day, n=10):
    ser = day.get("series") or {}
    top = []
    for b in day.get("boards") or []:
        s = ser.get(b.get("code"))
        if not s or not s.get("pcts"):
            continue
        try:
            p = float(s["pcts"][-1])
        except (TypeError, ValueError):
            continue
        top.append({"code": b.get("code"), "name": s.get("name") or b.get("name"),
                    "pct": round(p, 2)})
    top.sort(key=lambda x: x["pct"], reverse=True)
    return top[:n]


def _day_bottom(day, n=5):
    """当日跌幅最深的 n 个板块（升序取尾再反转为跌幅从重到轻），供轮动动画「跌TOP5」侧。"""
    ser = day.get("series") or {}
    rows = []
    for b in day.get("boards") or []:
        s = ser.get(b.get("code"))
        if not s or not s.get("pcts"):
            continue
        try:
            p = float(s["pcts"][-1])
        except (TypeError, ValueError):
            continue
        rows.append({"code": b.get("code"), "name": s.get("name") or b.get("name"),
                     "pct": round(p, 2)})
    rows.sort(key=lambda x: x["pct"])          # 最跌在前
    return rows[:n]


# ---- 轮动统计口径（2026-09-04 重定义，纯函数抽出便于单测） ----
# 旧口径的两个死穴（当日实测）：
#   速度 = 相邻两日「当日 Top10」的 Jaccard 距离 → 第10/11名涨幅差仅 0.01~0.09 点，
#          名单按抛硬币进出，速度常年钉死 0.89~1.0，毫无信息量；
#   持续强势 = 「连续 ≥3 日在榜」→ 极速轮动市里主线走「碎步+爆发」路径（如种植业
#          5日 -1.3/+7.5/-4.4/-1.3/+2.2 累计+16%），连续在榜口径常年给出空列表。
# 新口径：速度 = 相邻两日「近5日累计 Top10」主线集合的差异（5日累计滤掉单日排名噪声，
#   量纲不变，0.3 以下=主线稳定、0.8 以上=主线天天换）；持续强势 = 近10日窗口内
#   Top10 在榜 ≥4 日 且 累计 ≥5%（在榜看频次不看连续，累计看强度）。
SPEED_MAINLINE_N = 10       # 主线 = 近5日累计涨幅 Top10
SPEED_MAINLINE_WINDOW = 5   # 主线累计窗口（交易日）
PERSIST_WINDOW = 10         # 持续强势观察窗（交易日）
PERSIST_TOP_DAYS = 4        # 窗口内 Top10 在榜 ≥4 日
PERSIST_CUM_MIN = 5.0       # 窗口内累计涨幅 ≥5%
STRONG_DAY_PCT = 2.0        # 强势日门槛（与分时沉淀「强势日」口径一致）


def _board_pcts(day):
    """当日全板块收盘涨幅 {code: {name, pct}}（_day_tops 的全集版）。"""
    ser = day.get("series") or {}
    out = {}
    for b in day.get("boards") or []:
        s = ser.get(b.get("code"))
        if not s or not s.get("pcts"):
            continue
        try:
            p = float(s["pcts"][-1])
        except (TypeError, ValueError):
            continue
        out[b.get("code")] = {"name": s.get("name") or b.get("name"), "pct": round(p, 2)}
    return out


def _mainline_set(win, n=SPEED_MAINLINE_N):
    """观察窗内按累计涨幅取 Top n 的主线 code 集合。win = 逐日 _board_pcts 列表。"""
    cum = {}
    for d in win:
        for c, e in d.items():
            g = cum.setdefault(c, {"name": e["name"], "mul": 1.0})
            g["mul"] *= (1 + e["pct"] / 100)
    top = sorted(cum.items(), key=lambda kv: -kv[1]["mul"])[:n]
    return {c for c, _ in top}


def _jaccard_distance(a, b):
    """Jaccard 距离（1−相似度）；两集合皆空返回 None（无信息，不假装 0 或 1）。"""
    union = a | b
    if not union:
        return None
    return 1 - len(a & b) / len(union)


def _persistent_list(win, top_days=PERSIST_TOP_DAYS, cum_min=PERSIST_CUM_MIN,
                     strong_pct=STRONG_DAY_PCT):
    """持续强势：窗口内 Top10 在榜 ≥top_days 日 且 累计 ≥cum_min%。
    返回 [{code, name, days, cum, strong, window}]（按累计降序）；
    观察窗不足 5 日不给结论（阈值语义不成立，宁空勿滥）。"""
    if len(win) < 5:
        return []
    cum, topcnt, strong = {}, {}, {}
    for d in win:
        t10 = {c for c, _ in sorted(d.items(), key=lambda kv: -kv[1]["pct"])[:10]}
        for c, e in d.items():
            g = cum.setdefault(c, {"name": e["name"], "mul": 1.0})
            g["mul"] *= (1 + e["pct"] / 100)
            if c in t10:
                topcnt[c] = topcnt.get(c, 0) + 1
            if e["pct"] >= strong_pct:
                strong[c] = strong.get(c, 0) + 1
    out = []
    for c, g in cum.items():
        days_in = topcnt.get(c, 0)
        pct = (g["mul"] - 1) * 100
        if days_in >= top_days and pct >= cum_min:
            out.append({"code": c, "name": g["name"], "days": days_in,
                        "cum": round(pct, 1), "strong": strong.get(c, 0),
                        "window": len(win)})
    out.sort(key=lambda x: -x["cum"])
    return out


def compute_rotation_stats(limit=48):
    """与旧 server.rotation_stats 输出形状保持一致（API 兼容）。"""
    dates = _rot_dates()[-limit:]
    days = []
    for dstr in dates:
        try:
            with open(os.path.join(ROT_DAILY, dstr + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        days.append({"date": dstr, "top": _day_tops(d), "bottom": _day_bottom(d),
                     "pcts": _board_pcts(d)})
    empty = {"dates": [], "speed": [], "top10": {}, "persistent": [],
             "newcomers": [], "leaders_5d": [], "by_date": {}}
    if not days:
        return empty
    pcts_all = [d["pcts"] for d in days]
    # 轮动速度：相邻两日「近5日累计 Top10」主线集合的 Jaccard 距离；窗口不满 5 日不产出
    speed = []
    for i in range(SPEED_MAINLINE_WINDOW, len(days)):
        dist = _jaccard_distance(
            _mainline_set(pcts_all[i - SPEED_MAINLINE_WINDOW:i]),
            _mainline_set(pcts_all[i - SPEED_MAINLINE_WINDOW + 1:i + 1]))
        if dist is not None:
            speed.append({"date": days[i]["date"], "speed": round(dist, 3)})
    persistent = _persistent_list(pcts_all[-PERSIST_WINDOW:])
    newcomers = []
    if len(days) >= 2:
        yset = {x["code"] for x in days[-2]["top"]}
        newcomers = [x for x in days[-1]["top"] if x["code"] not in yset]
    leaders = []
    cum = {}
    for d in days[-5:]:
        for x in d["top"]:
            c = cum.setdefault(x["code"], {"name": x["name"], "mul": 1.0})
            c["mul"] *= (1 + x["pct"] / 100)
    leaders = [{"code": k, "name": v["name"], "pct": round((v["mul"] - 1) * 100, 2)}
               for k, v in cum.items()]
    leaders.sort(key=lambda x: x["pct"], reverse=True)
    # 多日轮动动画数据（#6）：每日 涨TOP5 + 跌TOP5（前端画成以 0 为中轴的蝶蝶形对比）
    race = [{"date": d["date"], "top": d["top"][:5], "bottom": d["bottom"]} for d in days]

    # 逐日锚定统计（#7 复盘页按快照日期取数）：顶层 persistent/newcomers/leaders_5d 仍锚定
    # 最后一日（首页用），by_date[date] 为「截至该日」的同口径统计，避免复盘换日期看到同一份。
    by_date = {}
    for i, d in enumerate(days):
        sp_hist = [s for s in speed if s["date"] <= d["date"]][-5:]
        sp_val = sp_hist[-1]["speed"] if sp_hist and sp_hist[-1]["date"] == d["date"] else None
        nc = []
        if i >= 1:
            yset = {x["code"] for x in days[i - 1]["top"]}
            nc = [x for x in d["top"] if x["code"] not in yset]
        pers = _persistent_list(pcts_all[max(0, i - PERSIST_WINDOW + 1):i + 1])
        cum = {}
        for dd in days[max(0, i - 4): i + 1]:
            for x in dd["top"]:
                cc = cum.setdefault(x["code"], {"name": x["name"], "mul": 1.0})
                cc["mul"] *= (1 + x["pct"] / 100)
        lead = sorted(({"code": k, "name": v["name"], "pct": round((v["mul"] - 1) * 100, 2)}
                       for k, v in cum.items()), key=lambda x: x["pct"], reverse=True)[:10]
        by_date[d["date"]] = {"speed": sp_val, "speed_hist": sp_hist,
                              "newcomers": nc, "persistent": pers, "leaders_5d": lead}

    return {
        "dates": [d["date"] for d in days],
        "speed": speed,
        "top10": {d["date"]: d["top"] for d in days},
        "persistent": persistent,
        "newcomers": newcomers,
        "leaders_5d": leaders[:10],
        "race": race,
        "by_date": by_date,
    }


_LAUNCH_BUCKETS = ["09:30-10:00", "10:00-10:30", "10:30-11:00", "11:00-11:30",
                   "13:00-13:30", "13:30-14:00", "14:00-14:30", "14:30-15:00"]
_PEAK_BUCKETS = _LAUNCH_BUCKETS


def _bucket_of(t):
    if not t or len(t) < 5:
        return None
    hm = t[:5]
    for b in _LAUNCH_BUCKETS:
        if b.split("-")[0] <= hm < (b.split("-")[1] if b != _LAUNCH_BUCKETS[-1] else "15:01"):
            return b
    return None


_matrix_day_cache = {}   # date -> {"mtime": float, "feat": {...}}


def _matrix_day_feat(dstr):
    """单日矩阵特征（各板块收盘/峰值/启动/尾盘30分/成交额 + 形态桶），按 (日期, mtime) 缓存。"""
    fp = os.path.join(ROT_DAILY, dstr + ".json")
    try:
        m = os.path.getmtime(fp)
    except OSError:
        return None
    ent = _matrix_day_cache.get(dstr)
    if ent and ent["mtime"] == m:
        return ent["feat"]
    try:
        with open(fp, encoding="utf-8") as f:
            day = json.load(f)
    except Exception:
        return None
    times = day.get("times") or []
    cov = day.get("coverage") or {}
    daily_only = bool(day.get("daily_only"))   # 日K回填日（仅开盘/收盘两点，src=ths-daily）
    feat = {
        "minutes": len(times),
        "last_time": cov.get("last_time") or (times[-1] if times else None),
        "skip": (not daily_only) and len(times) < 30,
        "boards": {},
    }
    if not feat["skip"]:
        tail_start = max(0, len(times) - 30)
        # 回填日（daily_only）时间标签是 开盘/收盘：归一到 09:30/15:00 才能进形态桶；
        # 且回填日只有两点，「启动时刻」仅当开盘已 ≥1% 才可知（否则盘中启动点不可知，置 None 不冒充）
        for b in day.get("boards") or []:
            code = b.get("code")
            s = (day.get("series") or {}).get(code)
            if not s:
                continue
            pcts = [p for p in (s.get("pcts") or []) if p is not None]
            if len(pcts) < (2 if daily_only else 30):
                continue
            name = s.get("name") or b.get("name") or code
            close_pct = round(float(pcts[-1]), 2)
            peak = max(pcts)
            s_times = s.get("times") or []
            s_times_norm = [{"开盘": "09:30", "收盘": "15:00"}.get(t, t) for t in s_times]
            # 回填日（仅开/收两点）盘中见顶时刻不可知：max(开,收) 落在收盘不代表 15:00 见顶
            # （盘中可能早已冲高回落），冒充成「尾盘见顶」会把 40+ 回填日的强势板全堆进
            # 14:30-15:00 桶（实测 665 次），压扁整张形态图——与 launch/tail 同口径诚实置 None。
            if daily_only:
                peak_t = None
            else:
                peak_t = s_times_norm[s["pcts"].index(peak)] if peak in s["pcts"] else None
            if daily_only:
                launch_t = "09:30" if pcts and pcts[0] >= 1.0 else None
            else:
                launch_t = next((t for t, p in zip(s_times_norm, pcts) if p >= 1.0), None)
            # 回填日没有盘中过程：尾盘30分/拉升不可算，置 null 而不是拿收盘-开盘冒充
            tail_delta = None if daily_only else round(float(pcts[-1]) - float(pcts[tail_start]), 2)
            lb = _bucket_of(launch_t) if close_pct >= 2.0 else None
            pb = _bucket_of(peak_t) if (close_pct >= 2.0 and peak_t) else None
            feat["boards"][code] = {
                "name": name, "close": close_pct, "peak": round(float(peak), 2),
                "peak_t": (peak_t or "")[:5], "launch_t": (launch_t or "")[:5],
                "tail30": tail_delta,
                "amt": round(sum(a for a in (s.get("amounts") or []) if a) / 1e8, 1),
                "launch_b": lb, "peak_b": pb,
                "rally": None if (daily_only or tail_delta is None)
                         else (tail_delta if (tail_delta >= 1.0 and len(pcts) >= 60) else None),
            }
    _matrix_day_cache[dstr] = {"mtime": m, "feat": feat}
    return feat


def compute_matrix(window=48):
    """板块×日强度矩阵 + 日内形态统计（基于已有分时，零抓取）。

    单日特征按 (date, mtime) 缓存，增量刷新只解析新增/变更的分时文件；
    输出与逐日全量解析版本完全一致。
    """
    dates = _rot_dates()[-window:]
    if not dates:
        return None
    board_days = {}   # code -> {name, rows:{date:{...}}}
    coverage = []
    launch_hist = {b: 0 for b in _LAUNCH_BUCKETS}
    peak_hist = {b: 0 for b in _PEAK_BUCKETS}
    tail_rallies = []
    for dstr in dates:
        feat = _matrix_day_feat(dstr)
        if feat is None:
            continue
        coverage.append({"date": dstr, "minutes": feat["minutes"],
                         "last_time": feat["last_time"]})
        if feat["skip"]:
            continue
        for code, e in feat["boards"].items():
            entry = board_days.setdefault(code, {"code": code, "name": e["name"], "rows": {}})
            entry["rows"][dstr] = {
                "close": e["close"], "peak": e["peak"],
                "peak_t": e["peak_t"], "launch_t": e["launch_t"],
                "tail30": e["tail30"], "amt": e["amt"],
            }
            if e["launch_b"]:
                launch_hist[e["launch_b"]] += 1
            if e["peak_b"]:
                peak_hist[e["peak_b"]] += 1
            if e["rally"] is not None:
                tail_rallies.append({"date": dstr, "code": code, "name": e["name"],
                                     "tail30": e["tail30"], "close": e["close"]})
    # 选板块：窗口内强势日（收盘≥2%）次数最多的 18 个
    tops_count = {}
    for code, e in board_days.items():
        tops_count[code] = sum(1 for r in e["rows"].values() if r["close"] >= 2.0)
    picked = sorted(tops_count.items(), key=lambda x: (-x[1], x[0]))[:18]
    boards_out = []
    for code, _n in picked:
        e = board_days[code]
        boards_out.append({
            "code": code, "name": e["name"],
            "cells": {d: e["rows"][d] for d in dates if d in e["rows"]},
        })
    tail_rallies.sort(key=lambda x: x["tail30"], reverse=True)
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": dates,
        "coverage": coverage,
        "boards": boards_out,
        "patterns": {
            "launch_hist": launch_hist,
            "peak_hist": peak_hist,
            "tail_rallies": tail_rallies[:15],
            "strong_day_rule": "收盘涨幅≥2% 计为强势日 · 回填日仅开/收两点：启动只在开盘≥1%时计入、见顶时刻不可知不计入",
        },
    }


def derive_rotation(force=False):
    os.makedirs(PANEL_ROT, exist_ok=True)
    stats_out = os.path.join(PANEL_ROT, "stats.json")
    matrix_out = os.path.join(PANEL_ROT, "matrix.json")
    dates = _rot_dates()
    src_max = _mtime_newest([os.path.join(ROT_DAILY, d + ".json") for d in dates])
    changed = False
    if force or _is_stale(stats_out, src_max):
        st = compute_rotation_stats()
        Path(stats_out).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        changed = True
    if force or _is_stale(matrix_out, src_max):
        mx = compute_matrix()
        if mx is not None:
            Path(matrix_out).write_text(json.dumps(mx, ensure_ascii=False), encoding="utf-8")
            changed = True
    if changed:
        print(f"derive: rotation panel -> {PANEL_ROT}")
    return changed


_last_check = 0.0


def ensure_fresh():
    """server 端调用：源数据比面板新时增量重算（含节流，60s 内不重复检查）。"""
    global _last_check
    now = time.time()
    if now - _last_check < 60:
        return
    _last_check = now
    try:
        derive_recap()
        derive_rotation()
    except Exception as e:
        print(f"derive ensure_fresh 失败: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="派生层重算")
    ap.add_argument("--force", action="store_true", help="强制全量重算")
    args = ap.parse_args()
    a = derive_recap(args.force)
    b = derive_rotation(args.force)
    if not (a or b):
        print("derive: 面板均为最新，跳过")


if __name__ == "__main__":
    main()
