# -*- coding: utf-8 -*-
"""序列缓存：基准指数/行业指数/情绪序列读取，含进程内 memo（index.history 增量缓存与 sentiment.csv 的统一收口）。"""
import bisect
import csv
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import industry_common  # noqa: E402  # backend/ 个股→一级行业共享映射读取器
import index_hist_cache  # noqa: E402  指数/行业指数日线增量缓存（boards 共用）
from spec_rules import INDEX_BENCH  # noqa: E402  基准指数口径（最底层常量模块）

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_SENTIMENT = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "data", "recap", "panel", "sentiment.csv")


# ---------------------------------------------------------------- 指数基准

def index_series(market, need_date8):
    """基准指数日线 [[date, close]...]（升序）；实现在 index_hist_cache（增量缓存，
    与 providers.boards / industry_series 共用同一份 .ht_cache 文件与进程内 memo）。"""
    code, _name = INDEX_BENCH[market]
    return index_hist_cache.series(code, need_date8)


def index_gain(rows, date_iso, n):
    """指数 n 个交易日区间涨幅（%）；当日或窗口不足返回 None。

    旧实现线性扫描 rows 两遍找「最后一个 ≤date_iso 的下标」（每次 O(n)，一次
    compute 内 g3/g10/g30 连扫）；改 bisect O(log n)，语义不变（rows 升序）。"""
    i = bisect.bisect_right(rows, date_iso, key=lambda r: r[0]) - 1
    if i < 0:
        return None
    if i - n < 0:
        return None
    return (rows[i][1] / rows[i - n][1] - 1) * 100.0


_ROT_BOARDS = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                           "data", "rotation", "boards.json")


def _industry_catalog():
    """行业名 → 881 代码（data/rotation/boards.json 全站单一目录）；缺失返回 {}。"""
    try:
        with open(_ROT_BOARDS, encoding="utf-8") as f:
            d = json.load(f)
        out = {}
        for b in (d.get("industry") or []):
            code = str(b.get("code") or "").split(".")[0]
            if code.startswith("881") and b.get("name"):
                out[b["name"]] = code
        return out
    except Exception:  # noqa: BLE001
        return {}


def _industry_lookup():
    """{code6: (ind_code881, 行业名)}；共享映射（≤5 天新鲜）× 轮动目录，缺一返回 {}。

    一次 compute 内 build_pool 与 validate_prev_pool 各调一次、上游文件不变——
    按（共享映射 mtime, boards.json mtime）做进程内 memo，文件变了自动失效。"""
    key = (_mtime_or_none(industry_common.SHARED_PATH),
           _mtime_or_none(_ROT_BOARDS))
    if _IND_LOOKUP_MEMO.get("key") == key:
        return _IND_LOOKUP_MEMO["value"]
    names = industry_common.load_shared_ticker_map()
    out = {}
    if names:
        cat = _industry_catalog()
        if cat:
            for code6, name in names.items():
                ic = cat.get(name)
                if ic:
                    out[code6] = (ic, name)
    _IND_LOOKUP_MEMO.update(key=key, value=out)
    return out


def _mtime_or_none(fp):
    try:
        return os.path.getmtime(fp)
    except OSError:
        return None


_IND_LOOKUP_MEMO = {"key": None, "value": None}


def industry_series(ind_code, need_date8):
    """行业指数日线 [[date, close]...]（升序）；实现在 index_hist_cache.series
    （.ht_cache 按 881 代码缓存 + 增量补尾 + checked_through 防呆，与大盘基准、
    providers.boards 同一收口），本函数只做 thscode 形态适配。"""
    code = str(ind_code).split(".")[0]
    return index_hist_cache.series(code + ".TI", need_date8)


def _series_pct(rows, date_iso):
    """行业指数 date_iso 当日涨幅（%）；该日不在序列内（未覆盖/停市）返回 None。"""
    i = bisect.bisect_right(rows, date_iso, key=lambda r: r[0]) - 1
    if i <= 0 or rows[i][0] != date_iso:
        return None
    c0, c1 = rows[i - 1][1], rows[i][1]
    return round((c1 / c0 - 1) * 100.0, 2) if c0 else None


# ---------------------------------------------------------------- 情绪序列

_SENT_ROWS_MEMO = {"mtime": None, "rows": None}


def read_sentiment_rows():
    """sentiment.csv → [{date, index, zt, dt, max_lb, promo_rate, ...}] 升序。

    一次 compute 内 build_cycle / _mf_env / validate_prev_pool 等共解析 3 遍同一段
    CSV——按文件 mtime 做进程内 memo（retrofill 多日期循环时文件不变即命中，
    derive 重算后 mtime 变化自动失效）。返回缓存共享列表，调用方只读。
    """
    try:
        m = os.path.getmtime(PANEL_SENTIMENT)
    except OSError:
        return []
    if _SENT_ROWS_MEMO["mtime"] == m and _SENT_ROWS_MEMO["rows"] is not None:
        return _SENT_ROWS_MEMO["rows"]
    rows = []
    with open(PANEL_SENTIMENT, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = str(r.get("date") or "").strip()
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"   # csv 是 8 位日期，统一成 ISO 再比较

            def num(k):
                try:
                    v = r.get(k)
                    return None if v in (None, "", "None") else float(v)
                except (TypeError, ValueError):
                    return None
            rows.append({
                "date": d, "index": num("index"),
                "label": r.get("label"), "zt": num("zt"), "dt": num("dt"),
                "max_lb": num("max_lb"), "promo_rate": num("promo_rate"),
                "up_ratio": num("up_ratio"), "zhaban": num("zhaban"),
            })
    rows.sort(key=lambda x: x["date"] or "")
    _SENT_ROWS_MEMO.update(mtime=m, rows=rows)
    return rows
