# -*- coding: utf-8 -*-
"""个股→同花顺一级行业映射的跨子系统读取器（2026-09-01 口径统一）。

单一来源：竞价采集器维护的 data/auction/industry_map.json
（auc_industry.py 每 3 天按 90 个一级行业成分重建，带行数防呆）。
复盘 providers 与全球 fetch_global 原来各自从同一份成分数据另建映射
（stock_industry_<DATE>.json，键为裸 6 位），是「同一事实两套供给」——
本模块把它们收拢为优先读共享映射，退回自建仅作竞价侧缺档的兜底。

判定新鲜度用 map 内 ts/built_at：超过 MAX_AGE_DAYS 视为不可信（新股缺口），
各消费方退回自建路径，宁新勿旧。
"""
import json
import os
import time

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
SHARED_PATH = os.path.join(PROJECT_ROOT, "data", "auction", "industry_map.json")
MAX_AGE_DAYS = 5


def _load_shared_doc():
    """读共享映射原始文档；文件缺失/结构异常返回 None。"""
    try:
        with open(SHARED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def load_shared_ticker_map():
    """{裸 6 位代码: 一级行业名}；文件缺失/过旧/结构异常返回 {}（调用方自备兜底）。"""
    d = _load_shared_doc()
    if not isinstance(d, dict):
        return {}
    m = d.get("map")
    if not isinstance(m, dict) or not m:
        return {}
    ts = d.get("ts") or 0
    if not ts:
        try:
            ts = time.mktime(time.strptime(str(d.get("built_at"))[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            ts = 0
    if not ts or (time.time() - ts) / 86400.0 > MAX_AGE_DAYS:
        return {}
    return {str(k).split(".")[0]: v for k, v in m.items() if v}


def shared_map_date8():
    """共享映射的数据日期（8 位口径单一来源）；缺失/异常返回 ""。

    fetch_global 等需要给数据标 vintage 的消费方用这个，
    不要拿「映射来自共享源」这一来源标签当日期写进产出。"""
    d = _load_shared_doc()
    if not isinstance(d, dict):
        return ""
    d8 = str(d.get("date8") or "")
    return d8 if len(d8) == 8 and d8.isdigit() else ""
