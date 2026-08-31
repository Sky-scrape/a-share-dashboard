# -*- coding: utf-8 -*-
"""板块钻取聚合：指定板块近 5 日走势 + 当日池内个股（原 server.py.board_members_payload，
2026-09-04 迁出 HTTP 层，兑现「HTTP 层不做业务计算」的既定边界）。

2026-09-01 起轮动与复盘同为同花顺一级行业口径（90 个，名称精确匹配），
旧的「东财↔同花顺」人工别名表与字符包含式模糊匹配已废除：
- 板块行按名称精确取当日复盘快照 boards 模块（同一份目录，exact 是唯一常态）；
- 池内个股按 industry_map.json（代码→一级行业，与竞价页同一份单一来源）归属；
  映射缺失的新股退回快照自带「所属行业」名称包含（宁缺勿错，match 不变）。
"""
import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "recap"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import snapio        # noqa: E402
import auc_industry  # noqa: E402
from modules import fget, cget  # noqa: E402

_MEMBER_TTL = 300.0
_LOCK = threading.Lock()
_CACHE = {"date": "", "ts": 0.0, "loaded": False}


def _load_snapshot(root):
    """最近复盘快照 → (date, by_name, pools, ind_by_ticker)。失败降级空结构。"""
    recap_data = os.path.join(root, "data", "recap")
    by_name, pools = {}, {"zt": [], "zb": [], "dt": []}
    cd = snapio.list_dates(recap_data)
    if cd:
        snap = snapio.load(cd[0], recap_data) or {}
        mod = (snap or {}).get("modules") or {}
        for r in ((mod.get("boards") or {}).get("data") or []):
            nm = r.get("名称") or ""
            if nm:
                by_name[nm] = r
        for tag, mk in (("zt", "limit_up_pool"), ("zb", "limit_break_pool"),
                        ("dt", "limit_down_pool")):
            for r in ((mod.get(mk) or {}).get("data") or []):
                code6 = str(cget(r, "code") or "").split(".")[0]
                pools[tag].append((code6, r))
    ind_by_ticker = {str(k).split(".")[0]: v
                     for k, v in auc_industry.load_map().items()}
    return (cd[0] if cd else ""), by_name, pools, ind_by_ticker


def payload(root, name):
    """返回指定板块近 5 日走势 + 当日池内个股（带 300s 进程内缓存，线程安全）。

    锁纪律：_LOCK 只护 _CACHE 的读写与过期判定；快照解析（几十 MB JSON）
    在锁外做，否则一次加载会阻塞所有并发钻取请求（后到者经双检直接复用
    先到者刷好的缓存）。加载失败沿用旧缓存；从未成功过则置空态且不更新
    ts——下个请求继续尝试重载（快照可能稍后就绪）。"""
    now = time.time()
    with _LOCK:
        fresh = _CACHE.get("loaded") and now - _CACHE["ts"] <= _MEMBER_TTL
    if not fresh:
        try:
            date, by_name, pools, ind = _load_snapshot(root)
        except Exception:  # noqa: BLE001 - 快照缺失/损坏时保持旧缓存或空态
            date, by_name, pools, ind = None, None, None, None
        with _LOCK:
            if date is None:
                if not _CACHE.get("loaded"):
                    _CACHE.update(date="", ts=0.0, by_name={}, pools={},
                                  ind={}, loaded=True)
            else:
                _CACHE.update(date=date, ts=now, by_name=by_name, pools=pools,
                              ind=ind, loaded=True)
            date = _CACHE["date"]
            by_name = _CACHE["by_name"]
            pools = _CACHE["pools"]
            ind_by_ticker = _CACHE.get("ind") or {}
    else:
        with _LOCK:
            date = _CACHE["date"]
            by_name = _CACHE["by_name"]
            pools = _CACHE["pools"]
            ind_by_ticker = _CACHE.get("ind") or {}

    row = by_name.get(name)
    match = "exact" if row is not None else "none"
    stocks, seen = [], set()
    for tag, rows in pools.items():
        for code6, r in rows:
            ind_name = ind_by_ticker.get(code6) or ""
            if ind_name:
                if ind_name != name:
                    continue
            else:
                row_ind = fget(r, "所属行业", "行业") or ""
                if not row_ind or (row_ind not in name and name not in row_ind):
                    continue
            code = cget(r, "code") or ""
            if not code or code in seen:
                continue
            seen.add(code)
            stocks.append({"pool": tag, "code": code, "name": cget(r, "name") or "",
                           "pct": cget(r, "pct"), "lb": cget(r, "lb"),
                           "seal": cget(r, "seal_amt")})
    return {"name": name, "query": name,
            "date": date, "match": match,
            "pct": (row.get("涨跌幅") if row else None),
            "members": ((row.get("history") or [])[-5:] if row else []),
            "stocks": stocks}
