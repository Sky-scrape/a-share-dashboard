# -*- coding: utf-8 -*-
"""providers 共享取数底座（2026-09-27 自 providers.py 拆出，行为零变化）。

拆包动机与总纪律见包 __init__.py。本文件收拢所有「跨数据模块共用」的取数
原语，让各数据子模块只留业务组装逻辑：

  - akshare 可选依赖守卫 require_ak 与重试门 _retry（3 次/3s，实现在
    backend/http_retry.py）
  - 错误封装 _err 与类型归一（_native/_norm_code/_records/_round2）
  - 按 DATE 键的进程内缓存门 _date_cached 与各缓存 dict（涨停/跌停/炸板池、
    热股榜、龙虎榜、概念快照、新浪指数 spot、全市场快照）
  - hithink 取数原语：全市场快照、三池、行业/概念目录、行业映射
  - 涨跌幅 9 区间单一来源 PCT_BINS（backfill_full 的全量回补分布同用这一份）

日期上下文 DATE/HISTORICAL 的真身在包 __init__.py（set_context 是唯一改写
入口）；本模块经 `import providers` 在调用时读包属性——外部直接改写
providers.DATE 的旧语义也因此原样保留（包部分初始化期间完成本模块导入，
循环导入安全）。
"""
import datetime as _dt
import json
import os
import sys

import numpy as np

# 包内路径锚点：拆包后 __file__ 深了一层，dirname 相应多一重；.ht_cache、
# fsutil、industry_common 的落点与拆分前逐字节一致（backend/recap/.ht_cache 等）。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))   # backend/recap/providers
_RECAP_DIR = os.path.dirname(_THIS_DIR)                  # backend/recap
_BACKEND_DIR = os.path.dirname(_RECAP_DIR)               # backend

try:
    import akshare as ak
except ImportError:  # 可选依赖：缺失时模块仍可导入，用到新浪/东财通道的模块单独降级报错
    ak = None

import ht
import http_retry
try:
    import fsutil            # 原子写盘单一来源（backend/fsutil.py）
except ImportError:          # 经 backfill 等只把 recap/ 入 path 的入口导入时兜底
    sys.path.insert(0, _BACKEND_DIR)
    import fsutil

import index_hist_cache     # noqa: E402  指数日线增量缓存（boards 与 speculate 共用）

# 抓取日期上下文：真身在包 __init__.py，这里只运行期读取（set_context 唯一改写入口）
import providers as _prov  # noqa: E402


def require_ak():
    """akshare 缺失时给出可行动的报错（而不是 AttributeError: NoneType）。"""
    if ak is None:
        raise RuntimeError("akshare 未安装（pip install akshare），该模块依赖新浪/东财通道")
    return ak


_RETRY_TIMES = 3      # 总共尝试 3 次（首次 + 2 次重试）
_RETRY_DELAY = 3      # 每次重试间隔秒数


def _retry(fn):
    """执行 fn，失败重试 2 次，仍失败抛最后一个异常（实现在 backend/http_retry.py）。

    现有调用点全部是 akshare 新浪/东财通道，顺手做依赖守卫：缺失时给出可行动
    报错而不是 AttributeError: NoneType；ht 通道失败不经这里，由 _err 直接降级。"""
    require_ak()
    return http_retry.retry(fn, tries=_RETRY_TIMES - 1, delay=_RETRY_DELAY)


def _err(e):
    """把异常转成 error 结果，保留真实类型与信息。"""
    return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}


# hithink 池子/榜单缓存：涨停/跌停/炸板池、热股榜、龙虎榜按 DATE 各抓一次。
# （2026-09-04 收拢：五份同构的「按 DATE 键缓存」实现合一，只留 _date_cached 一个门）
_HT_ZT_CACHE = {"date": None, "rows": None}
_HT_DT_CACHE = {"date": None, "rows": None}
_HT_ZB_CACHE = {"date": None, "rows": None}
_HT_HOT_CACHE = {"date": None, "rows": None}
_HT_LHB_CACHE = {"date": None, "rows": None}
# 概念指数当日快照（2026-09-10 收进同一扇门）：speculation bundles 与 fetch_all
# 的 concepts 模块同一天各调一次，此前会再抓一遍 390 概念。
_HT_CONCEPTS_CACHE = {"date": None, "rows": None}

# 新浪指数 spot（stock_zh_index_spot_sina）按 DATE 进程内 memo：market_indices 与
# breadth 两处同源各调一次，合并为一次共享（同一时刻口径还更一致）。
_INDEX_SPOT_CACHE = {"date": None, "df": None}

# 全市场快照缓存：breadth/extra/晋级率共用一次抓取（hithink market.snapshot）
_MKT_SNAP_CACHE = {"date": None, "rows": None, "by_code": None}


def _date_cached(cache, fetch):
    """按 DATE 键的取数缓存：命中返回行，未命中 fetch() 并写入。失败抛异常。"""
    if cache.get("date") == _prov.DATE and cache.get("rows") is not None:
        return cache["rows"]
    rows = fetch()
    cache.update(date=_prov.DATE, rows=rows)
    return rows


def index_spot_sina():
    """新浪指数 spot DataFrame（当日一次共享，失败抛异常由调用方降级）。"""
    require_ak()
    return _date_cached(_INDEX_SPOT_CACHE, lambda: ak.stock_zh_index_spot_sina())


def _round2(x):
    """安全保留两位小数（None 透传）。"""
    return None if x is None else round(float(x), 2)


def _cache_dir():
    return os.path.join(_RECAP_DIR, ".ht_cache")


def _get_market_snapshot():
    """hithink 全市场快照（带缓存，同一 DATE 只抓一次）。失败抛异常。"""
    def _fetch():
        rows = ht.market_snapshot_all()
        if not rows:
            raise RuntimeError("ValueError: 全市场快照为空")
        _MKT_SNAP_CACHE.update(by_code=None)
        return rows
    return _date_cached(_MKT_SNAP_CACHE, _fetch)


def _snap_by_code():
    """全市场快照的 ticker -> row 映射（随快照缓存）。"""
    if _MKT_SNAP_CACHE.get("by_code") is None:
        _MKT_SNAP_CACHE["by_code"] = {
            str(r.get("ticker")): r for r in _get_market_snapshot()}
    return _MKT_SNAP_CACHE["by_code"]


def _get_zt_pool_ht():
    """hithink 涨停池（带缓存）。失败抛异常。"""
    return _date_cached(_HT_ZT_CACHE, lambda: ht.pool_all(
        ["special", "limit-up-pool", "--date-ms", str(ht.date_ms(_prov.DATE))]))


def _get_dt_pool_ht():
    """hithink 跌停池（带缓存）。失败抛异常。"""
    return _date_cached(_HT_DT_CACHE, lambda: ht.pool_all(
        ["special", "limit-down-pool", "--date-ms", str(ht.date_ms(_prov.DATE))]))


def _get_zb_pool_ht():
    """hithink 炸板池（带缓存）。失败抛异常。"""
    return _date_cached(_HT_ZB_CACHE, lambda: ht.pool_all(
        ["special", "limit-break-pool", "--date-ms", str(ht.date_ms(_prov.DATE))]))


def _get_industries():
    """同花顺一级行业 [(thscode, name)]（881xxx，本地缓存 7 天）。"""
    items = ht.catalog("industry", cache_dir=_cache_dir())
    ind = [(str(x["thscode"]), x["name"]) for x in items
           if str(x.get("thscode", "")).startswith("881")]
    if not ind:
        raise RuntimeError("ValueError: 行业目录为空")
    return ind


def _get_industry_map():
    """ticker -> 一级行业名称 映射。

    2026-09-01 口径统一：优先读全站单一来源（竞价采集器维护的
    data/auction/industry_map.json，见 backend/industry_common）；
    缺失/过旧才退回兜底自建——该兜底逻辑（含 stock_industry_<DATE>.json 缓存
    与「保留最近 5 份」日落清理）2026-09-10 下沉到 industry_common.build_ticker_map，
    与 fetch_global 共用一处，本模块只调它。"""
    try:
        import industry_common
    except ImportError:  # 单独被 import 时 backend 根可能不在 sys.path
        sys.path.insert(0, _BACKEND_DIR)
        import industry_common
    shared = industry_common.load_shared_ticker_map()
    if shared:
        return shared
    return industry_common.build_ticker_map(_cache_dir(), _prov.DATE)


def _get_concepts():
    """同花顺概念目录 [(thscode, name)]（本地缓存 7 天）。"""
    items = ht.catalog("cn_concept", cache_dir=_cache_dir())
    out = [(str(x["thscode"]), x["name"]) for x in items]
    if not out:
        raise RuntimeError("ValueError: 概念目录为空")
    return out


# 涨跌幅区间分布（同花顺口径 9 区间：涨停 → 跌停）。
# 单一来源：backfill_full 的全量回补分布同用这一份（此前两份各自维护，
# 且 0~1% 下界一处 0.01 一处 1e-6，小于 0.01% 的微涨会被漏计）。
PCT_BINS = [
    ("涨停", 9.9, 999), ("涨停-5%", 5, 9.9), ("5-1%", 1, 5),
    ("1-0%", 1e-6, 1), ("平盘", 0, 0), ("0-1%", -1, 0),
    ("1-5%", -5, -1), ("5%-跌停", -9.9, -5), ("跌停", -999, -9.9),
]


def _native(x):
    """把 numpy 标量 / NaN 转成原生 Python 类型，保证 json 可序列化。"""
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, (_dt.date, _dt.datetime)):
        return x.isoformat()
    if isinstance(x, float) and x != x:  # NaN
        return None
    if isinstance(x, np.ndarray):
        return [_native(v) for v in x.tolist()]
    return x


def _norm_code(x):
    """证券代码归一化：'sh600519' -> '600519'；纯数字原样返回。"""
    s = str(x).strip().lower()
    if s.startswith(("sh", "sz", "bj")) and len(s) > 2:
        return s[2:]
    return s


def _records(df):
    """DataFrame → 可 JSON 序列化的 dict 列表（逐字段转换）。"""
    return [{k: _native(v) for k, v in r.items()} for r in df.to_dict("records")]
