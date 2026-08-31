# -*- coding: utf-8 -*-
"""个股实时行情服务（原 server.py.quote_upstream/quote_cached，2026-09-04 迁出 HTTP 层）。

同花顺个股实时快照（hithink market snapshot，90 只/批）+ 服务端 TTL 缓存。
2026-09-04 改进：缓存从「整串代码为 key」改为**单代码粒度**——旧实现里自选
列表增删一只就整体 miss、整批重抓；现在只补抓缺失/过期的代码，命中部分
直接复用，轮询风暴下的上游压力大幅下降。上游失败时回退过期缓存（宁旧勿空），
一个可用行都没有才报 error。
"""
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "recap"), os.path.join(_HERE, "auction"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ht  # noqa: E402
import auc_industry  # noqa: E402
from thscodes import to_thscode  # noqa: E402

TTL = 5.0                  # 缓存秒数（防前端轮询风暴）
BATCH = 90                 # hithink snapshot 单批上限（契约留余量）
MAX_KEYS = 2000            # 防膨胀上限
_LOCK = threading.Lock()
_CACHE = {}                # thscode -> (ts, row{code,name,price,pct,industry})


def _fetch_rows(ths_missing, root):
    """批量抓缺失代码，返回 {thscode: 上游行}；网络/CLI 异常时返回已收到的部分或 None。"""
    rows = {}
    try:
        for i in range(0, len(ths_missing), BATCH):
            d = ht.ht("market", "snapshot", "--thscodes",
                      ",".join(ths_missing[i:i + BATCH]), timeout=45)
            for x in (d.get("item") or []):
                rows[str(x.get("thscode"))] = x
    except Exception:  # noqa: BLE001 - 上游失败由调用方回退过期缓存
        return rows or None
    return rows


def _row_out(x, ind_map, names):
    tk = str(x.get("thscode") or "").split(".")[0]
    return {"code": tk,
            "name": (names or {}).get(tk) or "",
            "price": x.get("last_price"),
            "pct": x.get("price_change_ratio_pct"),
            "industry": ind_map.get(x.get("thscode")) or ""}


def quote_payload(codes, root):
    """入参可为裸 6 位/带 sh·sz·bj 前缀，输出 code 统一为裸 6 位（前端按原键回查）。"""
    ths, seen = [], set()
    for c in codes:
        t = to_thscode(c)
        if t and t not in seen:
            seen.add(t)
            ths.append(t)
    if not ths:
        return {"error": "无有效代码"}

    now = time.time()
    with _LOCK:
        fresh = {t for t, ent in _CACHE.items() if now - ent[0] < TTL}

    missing = [t for t in ths if t not in fresh]
    upstream_called = False
    stale = False
    if missing:
        got = _fetch_rows(missing, root) or {}
        if got:
            upstream_called = True
            try:
                names = ht.symbol_names(cache_dir=os.path.join(root, "data", "cache"))
            except Exception:  # noqa: BLE001 - 名称缺失不阻断行情
                names = {}
            ind_map = auc_industry.load_map()
            now = time.time()
            with _LOCK:
                for t, x in got.items():
                    _CACHE[t] = (now, _row_out(x, ind_map, names))
                if len(_CACHE) > MAX_KEYS:     # 简单防膨胀：淘汰最旧一半
                    for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:len(_CACHE) // 2]:
                        _CACHE.pop(k, None)
        elif _CACHE:
            stale = True   # 上游失败：正在用过期缓存兜底（宁旧勿空），如实标注
        else:
            return {"error": "上游未返回行情"}

    # 组装：缓存里有什么给什么（fresh 是刚抓的；上游失败时过期缓存兜底，宁旧勿空）
    with _LOCK:
        out = [dict(_CACHE[t][1]) for t in ths if t in _CACHE]
    payload = {"list": out, "src": "ths"}
    if stale:
        payload["stale"] = True    # 本次含过期兜底数据（前端当前不消费，仅供诊断/未来降级提示）
    elif not upstream_called:
        payload["cached"] = True
    return payload
