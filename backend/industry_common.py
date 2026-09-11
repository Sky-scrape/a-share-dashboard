# -*- coding: utf-8 -*-
"""个股→同花顺一级行业映射的跨子系统读取器（2026-09-01 口径统一）。

单一来源：竞价采集器维护的 data/auction/industry_map.json
（auc_industry.py 每 3 天按 90 个一级行业成分重建，带行数防呆）。
复盘 providers 与全球 fetch_global 原来各自从同一份成分数据另建映射
（stock_industry_<DATE>.json，键为裸 6 位），是「同一事实两套供给」——
本模块把它们收拢为优先读共享映射，退回自建仅作竞价侧缺档的兜底。

判定新鲜度用 map 内 ts/built_at：超过 MAX_AGE_DAYS 视为不可信（新股缺口），
各消费方退回自建路径，宁新勿旧。

2026-09-10 二次收拢：providers 的兜底自建（_get_industry_map 后半段）与
fetch_global 的「读最近一份历史缓存」（_latest_industry_map 后半段）都下沉到
本模块（build_ticker_map / latest_cached_ticker_map），三套实现归一处；
兜底缓存 stock_industry_*.json 加「保留最近 FALLBACK_KEEP 份」日落清理
（此前从不清理，已积 20+ 份）。
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from fsutil import save_json_atomic   # 原子写盘单一来源（backend/fsutil.py）

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
SHARED_PATH = os.path.join(PROJECT_ROOT, "data", "auction", "industry_map.json")
MAX_AGE_DAYS = 5
FALLBACK_KEEP = 5           # 兜底自建缓存保留份数（按文件名日期倒序）


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


# ---------------- 兜底路径（原 providers / fetch_global 各自的实现，2026-09-10 下沉） ----------------

def _ht():
    """ht CLI 封装延迟导入（在 backend/recap/ht.py，仅兜底自建路径需要）。"""
    try:
        import ht
        return ht
    except ImportError:
        sys.path.insert(0, os.path.join(BACKEND_DIR, "recap"))
        import ht
        return ht


def build_ticker_map(cache_dir, date8):
    """兜底自建 {裸6位代码: 一级行业名}（原 providers._get_industry_map 后半段）。

    共享主源缺失/过旧时由调用方走到这里：按 90 个一级行业成分反查
    （ht index constituents，8 线程），落盘 <cache_dir>/stock_industry_<date8>.json
    （原子写，保留最近 FALLBACK_KEEP 份）。缓存当日命中直接返回；整网失败抛
    RuntimeError 由调用方按模块失败降级。
    """
    ht = _ht()
    cache_file = os.path.join(cache_dir, f"stock_industry_{date8}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    items = ht.catalog("industry", cache_dir=cache_dir)
    industries = [(str(x["thscode"]), x["name"]) for x in items
                  if str(x.get("thscode", "")).startswith("881")]
    if not industries:
        raise RuntimeError("ValueError: 行业目录为空")
    name_by_code = dict(industries)
    mapping = {}

    def fetch(code):
        try:
            d = ht.ht("index", "constituents", "--thscode", code, timeout=60)
            return code, [str(x.get("ticker")) for x in (d.get("item") or [])]
        except Exception:  # noqa: BLE001 - 单行业失败跳过
            return code, []

    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, tickers in ex.map(fetch, [c for c, _ in industries]):
            for t in tickers:
                mapping.setdefault(t, name_by_code.get(code, ""))
    if not mapping:
        raise RuntimeError("ValueError: 行业成分映射为空")
    os.makedirs(cache_dir, exist_ok=True)
    save_json_atomic(cache_file, mapping)
    _prune_fallback_cache(cache_dir)
    return mapping


def _prune_fallback_cache(cache_dir):
    """兜底缓存日落：stock_industry_*.json 只留最近 FALLBACK_KEEP 份（失败不抛）。"""
    try:
        files = sorted(f for f in os.listdir(cache_dir)
                       if f.startswith("stock_industry_") and f.endswith(".json"))
        for name in files[:-FALLBACK_KEEP]:
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass
    except OSError:
        pass


def latest_cached_ticker_map(cache_dir):
    """读最近一份兜底缓存（原 fetch_global._latest_industry_map 后半段）。

    返回 (map|None, date8|None)——仅供补抓旧日期等兼容路径标注 vintage 用，
    不做任何网络请求。"""
    if not os.path.isdir(cache_dir):
        return None, None
    files = sorted(f for f in os.listdir(cache_dir)
                   if f.startswith("stock_industry_") and f.endswith(".json"))
    if not files:
        return None, None
    fp = os.path.join(cache_dir, files[-1])
    with open(fp, encoding="utf-8") as f:
        return json.load(f), files[-1][15:23]
