# -*- coding: utf-8 -*-
"""竞价全天异动回算（原 server.py._auction_alerts，2026-09-04 迁出 HTTP 层）。

「HTTP 层不做业务计算」是本项目既定边界；异动回算是有实际计算量的业务逻辑，
不该住在请求 handler 里。口径与前端 detectAlerts 完全一致：
|Δ涨幅|≥1.5pct 或匹配量跳升>1.5×且前值>20，只认窗口内轮（in_window），
可撤单期（09:15–09:20）轮带噪声标。

异动提醒原是纯前端内存态：只在页面开着的两次轮询之间环比，刷新即清零、
盘后必空。这里离线回算后随 /api/auction 下发，盘中是实时+回算合并，
盘后/刷新后也不丢。series.json 未变直接走缓存（文件不变零开销）。
"""
import os
import threading

import auc_config  # backend/auction 同目录

_LOCK = threading.Lock()
_CACHE = {"mtime": -1.0, "payload": None}


def _fmt_pct(v):
    return ("+" if v > 0 else "") + "%.2f%%" % v


def _fmt_vol(hand):
    if hand is None:
        return "-"
    try:
        hand = float(hand)
    except (TypeError, ValueError):
        return "-"
    if abs(hand) >= 1e4:
        return "%.1f万手" % (hand / 1e4)
    return "%g手" % hand


def compute(data):
    """对 series.json 的内容回算异动清单（纯函数，便于测试）。"""
    rounds = data.get("rounds") or []
    out, prev = [], {}
    for rnd in rounds:
        inw = rnd.get("in_window")
        if not isinstance(inw, bool):
            hms = str(rnd.get("ts") or "")[11:19]
            inw = len(hms) == 8 and "09:15:00" <= hms <= "09:25:59"
        if not inw:
            continue
        ts = str(rnd.get("ts") or "")
        t = ts[11:19]
        noise = len(t) == 8 and "09:15:00" <= t < "09:20:00"   # 可撤单期（前端 phaseInfo 同口径）
        for r in rnd.get("items") or []:
            tc = r.get("thscode")
            p0 = prev.get(tc)
            prev[tc] = r
            if not p0 or r.get("auction_pct") is None or p0.get("auction_pct") is None:
                continue
            dp = r["auction_pct"] - p0["auction_pct"]
            v0 = p0.get("auction_volume") or 0
            v1 = r.get("auction_volume") or 0
            vol_jump = v0 > 20 and v1 > v0 * 1.5
            if abs(dp) < 1.5 and not vol_jump:
                continue
            msg = (_fmt_pct(p0["auction_pct"]) + "→" + _fmt_pct(r["auction_pct"]) + " " if abs(dp) >= 1.5 else "") + \
                  ("匹配量 " + _fmt_vol(v0) + "→" + _fmt_vol(v1) if vol_jump else "")
            out.append({"t": t, "code": tc, "name": r.get("name") or r.get("ticker") or tc,
                        "noise": noise, "msg": msg.rstrip()})
    out.reverse()   # 最新在前（与前端 fresh.concat(state.alerts) 顺序一致）
    return {"date": data.get("date"), "items": out[:30]}


def payload(root):
    """按 series.json mtime 缓存的全天异动回算（server /api/auction 消费）。"""
    p = os.path.join(root, "data", "auction", "series.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = -1.0
    with _LOCK:
        if _CACHE["payload"] is not None and _CACHE["mtime"] == mt:
            return _CACHE["payload"]
    data = auc_config.load_json("series.json") or {}
    result = compute(data)
    with _LOCK:
        _CACHE.update(mtime=mt, payload=result)
    return result
