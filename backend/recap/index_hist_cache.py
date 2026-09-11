# -*- coding: utf-8 -*-
"""指数/行业指数日线增量缓存 · 单一收口（2026-09-10）。

此前三处各自为政：
  speculate.index_series     大盘基准（000001.SH / 399001.SZ），1500 天缓存；
  speculate.industry_series  881xxx 一级行业（增量补尾 + checked_through 防呆）；
  providers.boards           90 个行业每天各拉一遍 10 日 history（90 个子进程/天）。
本模块把「.ht_cache 按 thscode 缓存 + 增量补尾 + checked_through」收成一处，
boards() 与 speculate 共用同一份缓存文件：首次全量回溯 1500 天，之后每日只补
缓存尾段之后的新数据（子进程载荷从全量降到几个点）；非交易日拿不到新数据时
不再对同一 need 日反复请求。

缓存文件：backend/recap/.ht_cache/index_hist_<code>.json
  {"ts", "code", "rows": [["YYYY-MM-DD", close], ...升序], "checked_through": need}
文件名沿用两份旧实现的命名（.TI 后缀剥掉 / 其余点换下划线），存量缓存不重抓。

进程内 memo：按 (文件, mtime) 键——retrofill 多日期循环 / boards+speculate 同日
多次调用时同一序列只读一次盘；文件被增量续写后 mtime 变化自动失效。返回的
rows 是缓存共享对象，调用方只读、不得原地修改。

并发：providers.boards 用 ThreadPool 并发调 series()，各 thscode 文件互不重叠；
memo dict 读写依赖 GIL 原子性，无锁。写盘走 backend/fsutil 原子写。
"""
import datetime as _dt
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_BACKEND_ROOT = os.path.dirname(_HERE)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import fsutil  # noqa: E402
import ht      # noqa: E402  hithink CLI 封装（backend/recap/ht.py）

CACHE_DIR = os.path.join(_HERE, ".ht_cache")
LOOKBACK_DAYS = 1500          # 首次全量回溯窗口（与旧 index_series/industry_series 一致）

_MEMO = {}                    # fp -> (mtime, rows, checked_through)


def _date_iso(date8):
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"


def _file_key(thscode):
    """缓存文件名键：沿用旧命名（881101.TI -> 881101；000001.SH -> 000001_SH）。"""
    c = str(thscode)
    if c.endswith(".TI"):
        return c[:-3]
    return c.replace(".", "_")


def _fp_of(thscode):
    return os.path.join(CACHE_DIR, "index_hist_" + _file_key(thscode) + ".json")


def series(thscode, need_date8, timeout=60):
    """[[date_iso, close]...]（升序，覆盖 need_date8）；缓存命中不发起任何请求。

    thscode 形如 "000001.SH"（基准指数）或 "881101.TI"（一级行业）。
    """
    code = str(thscode)
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = _fp_of(code)
    need = _date_iso(need_date8)

    rows, checked = [], ""
    try:
        m = os.path.getmtime(fp)
    except OSError:
        m = None
    else:
        ent = _MEMO.get(fp)
        if ent and ent[0] == m:
            rows, checked = ent[1], ent[2]
        else:
            try:
                with open(fp, encoding="utf-8") as f:
                    saved = json.load(f)
                rows = saved.get("rows") or []
                checked = saved.get("checked_through") or ""
            except Exception:  # noqa: BLE001 - 缓存坏就重抓
                rows, checked = [], ""
            _MEMO[fp] = (m, rows, checked)

    if rows and (rows[-1][0] >= need or checked >= need):
        return rows

    # ---- 增量补尾：有缓存只取尾段之后；无缓存全量回溯 1500 天 ----
    if rows:
        start_ms = ht.date_ms(rows[-1][0].replace("-", "")) + 86400 * 1000
    else:
        start8 = (_dt.datetime.strptime(need, "%Y-%m-%d")
                  - _dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
        start_ms = ht.date_ms(start8)
    d = ht.ht("index", "history", "--thscode", code,
              "--start-ms", str(start_ms),
              "--end-ms", str(ht.date_ms(need_date8) + 86400 * 1000),
              timeout=timeout)
    new = [[_dt.datetime.fromtimestamp(r["date_ms"] / 1000).strftime("%Y-%m-%d"),
            float(r["close_price"])]
           for r in (d.get("item") or []) if r.get("close_price")]
    if not rows and not new:
        raise RuntimeError(f"index.history {code} 为空")
    merged = {r[0]: r[1] for r in rows}
    for dd, cc in new:
        merged[dd] = cc
    rows = [[k, v] for k, v in sorted(merged.items())]
    fsutil.save_json_atomic(fp, {"ts": time.time(), "code": code, "rows": rows,
                                 "checked_through": need})
    try:
        m = os.path.getmtime(fp)
    except OSError:
        m = None
    _MEMO[fp] = (m, rows, need)
    return rows
