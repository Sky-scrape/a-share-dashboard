# -*- coding: utf-8 -*-
"""DuckDB 层：_db_export 落地导出 + 全市场 SQL 扫描构建器（v_daily_qfq 十年日线，CLI 只认相对路径）。"""
import datetime as _dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import ht  # noqa: E402
import fsutil  # noqa: E402  原子写盘单一来源（backend/fsutil.py）
import spec_ohlc_fb  # noqa: E402  研究库日线缺口备源（腾讯前复权）
from spec_rules import SCAN_TH, _date_iso, thscode_of  # noqa: E402  最底层口径/工具

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".ht_cache")
# 日线层探测状态（server /api/health 只读不 import 本模块；ok=False=整层/查询缺口）
DUCKDB_STATUS = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                             ".status", "duckdb.json")   # 项目根 .status（server load_status 同源）


# ---------------------------------------------------------------- DuckDB

def _db_export(sql, tag):
    """db export 到 .ht_cache 相对路径，读回 ndjson 行（CLI 只认相对路径）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_abs = os.path.join(CACHE_DIR, f"{tag}.ndjson")
    out_rel = os.path.relpath(out_abs, os.getcwd())
    try:
        ht.ht("db", "export", "--sql", sql, "--output", out_rel, timeout=180)
        rows = []
        with open(out_abs, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    finally:
        try:
            os.remove(out_abs)
        except OSError:
            pass


def scan_deviation(date8, extra_thscodes=()):
    """全市场偏离值候选扫描（单日）。返回 [{thscode, r1, r3, r10, r30}]。"""
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=80)).strftime("%Y-%m-%d")
    in_sql = ""
    codes = sorted(set(extra_thscodes))
    if codes:
        in_sql = " OR thscode IN (" + ", ".join(f"'{c}'" for c in codes) + ")"
    sql = (
        "WITH w AS (SELECT thscode, date, close, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r1, "
        "close/NULLIF(LAG(close,3) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r3, "
        "close/NULLIF(LAG(close,10) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r10, "
        "close/NULLIF(LAG(close,30) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r30 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}') "
        "SELECT thscode, ROUND(r1*100,2) r1, ROUND(r3*100,2) r3, "
        "ROUND(r10*100,2) r10, ROUND(r30*100,2) r30 "
        f"FROM w WHERE date = DATE '{d}' AND ({SCAN_TH}{in_sql})"
    )
    return _db_export(sql, f"devscan_{date8}")


def day_pcts(date8, thscodes):
    """指定个股当日涨跌幅（%）：{thscode: pct}。"""
    if not thscodes:
        return {}
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=15)).strftime("%Y-%m-%d")
    in_sql = ", ".join(f"'{c}'" for c in sorted(set(thscodes)))
    sql = (
        "WITH w AS (SELECT thscode, date, close, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r1 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        f"SELECT thscode, ROUND(r1*100,2) pct FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"fatepct_{date8}")
    return {r["thscode"]: r.get("pct") for r in rows}


def _prev_yizi_codes(date8, sent_rows):
    """上一交易日一字板代码集合（open==high==low==close，DuckDB 前复权口径）。

    负面清单（M_Final 起冻结）：昨日一字板不接力。无上一交易日数据时返回 None（不启用该排除）。
    """
    iso = _date_iso(date8)
    prevs = [r["date"] for r in sent_rows if (r["date"] or "") < iso]
    if not prevs:
        return None
    try:
        rows = _db_export(
            "SELECT thscode FROM v_daily_qfq WHERE date = DATE '" + prevs[-1] + "' "
            "AND open = high AND high = low AND low = close", f"prevyizi_{date8}")
    except Exception:  # noqa: BLE001 - 一字排除失败不阻断选股
        return None
    return {str(r.get("thscode") or "").split(".")[0] for r in rows}


def _market_amt_ratio(date8):
    """T 日全市场成交额 / 前 19 个交易日均值（DuckDB 单查询）。

    与 engine ms.amt_ratio 同口径（T 日收盘可得，无未来函数）；数据缺失返回
    None——闸门不启用（诚实降级）。本地库落后（T 日 bar 缺失）时同样返回 None：
    拿 T-1 当 T 计算会静默用错日数据（0914 实例，「市场量能 0.98」实为 0911 口径）。"""
    iso = _date_iso(date8)
    try:
        rows = _db_export(
            "SELECT date, SUM(amount) AS amt FROM v_daily_qfq "
            f"WHERE date <= DATE '{iso}' GROUP BY date ORDER BY date DESC LIMIT 20",
            f"amtratio_{date8}")
    except Exception:  # noqa: BLE001
        return None
    if not rows or str((rows[0] or {}).get("date") or "")[:10] != iso:
        return None
    amts = [r.get("amt") for r in rows if r.get("amt")]
    if len(amts) < 20 or not amts[0]:
        return None
    base = sum(amts[1:]) / 19.0
    return amts[0] / base if base else None


def _lu_volr(date8, codes):
    """涨停池个股当日量比（volume / 含当日前5日均量，strategy-iter 引擎同口径）。"""
    if not codes:
        return {}
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=20)).strftime("%Y-%m-%d")
    in_sql = ", ".join("'" + thscode_of(c) + "'" for c in sorted(set(codes)))
    sql = (
        "WITH w AS (SELECT thscode, date, volume, "
        "AVG(volume) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) v5 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        "SELECT thscode, ROUND(volume/NULLIF(v5,0),2) volr "
        f"FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"luvolr_{date8}")
    return {str(r.get("thscode") or "").split(".")[0]: r.get("volr") for r in rows}


def scan_trend(date8):
    """非涨停组趋势/位置/动量/流动性全市场扫描（DuckDB 单日，主板范围在 Python 侧过滤）。

    返回 [{thscode, pct, close, ma10, ma20, ma20p, ma60, high60, gain20, volr, amount, amt5}]。
    硬筛对应非涨停组（M_Final 起冻结）：当日涨幅 -3~+7%、多头排列（close>ma20>ma60*0.995）、
    上市 ≥120 根 bar（400 日窗口内计数）、成交 ≥10 亿。
    """
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=400)).strftime("%Y-%m-%d")
    sql = (
        "WITH w1 AS (SELECT thscode, date, close, volume, amount, "
        "COUNT(*) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) bars, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS pct, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) ma10, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) ma20, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) ma60, "
        "MAX(high) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) high60, "
        "LAG(close,20) OVER (PARTITION BY thscode ORDER BY date) c20, "
        "AVG(volume) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) vol5, "
        "AVG(amount) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) amt5 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}'), "
        "w2 AS (SELECT *, LAG(ma20,1) OVER (PARTITION BY thscode ORDER BY date) ma20p FROM w1) "
        "SELECT w2.thscode, s.name, ROUND(pct*100,2) pct, close, ma10, ma20, ma20p, ma60, high60, "
        "ROUND((close/NULLIF(c20,0)-1)*100,2) gain20, ROUND(volume/NULLIF(vol5,0),2) volr, amount, amt5 "
        f"FROM w2 JOIN v_symbol s USING (thscode) WHERE date = DATE '{d}' AND w2.thscode NOT LIKE '%.BJ' "
        "AND bars >= 120 AND amount >= 1000000000 AND pct BETWEEN -0.03 AND 0.07 "
        "AND close > ma20 AND ma20 > ma60 * 0.995"
    )
    return _db_export(sql, f"trendscan_{date8}")


def ohlc_rets(date8, codes):
    """T 日相对 T-1 收盘的开/高/低/收涨幅（%）与量比：{code6: {o,h,l,c,amtr}}。

    本地库缺行（整层未同步/个股缺口/查询失败）时走腾讯前复权日线备源兜底（spec_ohlc_fb，
    前复权同口径），补到的行带 src="em"；两源都缺的行如实缺席（真停牌/退市）。
    每次调用把研究库日线层状态原子落 .status/duckdb.json（server health 告警，
    2026-09-15：0914 整层缺失曾静默降级成「停牌」标签且无任何监控）。"""
    codes = [str(c) for c in codes if str(c)]
    if not codes:
        return {}
    out, db_error = {}, None
    try:
        out = _duckdb_ohlc(date8, codes)
    except Exception as e:  # noqa: BLE001 - 库不可用不丢验证：整体走备源
        db_error = f"{type(e).__name__}"
    missing = [c for c in codes if c not in out]
    fb = {}
    if missing and len(missing) <= _FB_MAX_CODES:
        try:
            fb = spec_ohlc_fb.day_rets(date8, missing)
        except Exception:  # noqa: BLE001 - 备源失败不影响既有行为
            fb = {}
    out.update(fb)
    if missing:
        layer_n, last_bar = _layer_probe(date8)
    else:
        layer_n, last_bar = None, _date_iso(date8)   # 标的齐全 → T 日层必在（下界）
    fallback = ({"n": len(missing), "ok": len(fb),
                 "codes": sorted(missing)[:10]} if missing else None)
    note = (f"本地研究库缺 {last_bar or '-'} 之后的日线层"
            if layer_n == 0 else
            (f"本地研究库查询失败（{db_error}）" if db_error else
             (f"本地研究库缺 {len(missing)} 只标的日线" if missing else
              "标的日线齐全（本地研究库）")))
    _record_layer_status(date8, layer_n, last_bar, fallback, db_error, note)
    return out


def _duckdb_ohlc(date8, codes):
    """DuckDB 主源：T 日相对 T-1 收盘的 o/h/l/c 涨幅（%）与量比（LAG 前一根 bar）。"""
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=15)).strftime("%Y-%m-%d")
    in_sql = ", ".join("'" + thscode_of(c) + "'" for c in sorted(set(codes)))
    sql = (
        "WITH w AS (SELECT thscode, date, open, high, low, close, amount, "
        "LAG(close,1) OVER (PARTITION BY thscode ORDER BY date) pc, "
        "LAG(amount,1) OVER (PARTITION BY thscode ORDER BY date) pa "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        "SELECT thscode, ROUND((open/NULLIF(pc,0)-1)*100,2) o, ROUND((high/NULLIF(pc,0)-1)*100,2) h, "
        "ROUND((low/NULLIF(pc,0)-1)*100,2) l, ROUND((close/NULLIF(pc,0)-1)*100,2) c, "
        "ROUND(amount/NULLIF(pa,0),2) amtr "
        f"FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"poolval_{date8}")
    return {str(r.get("thscode") or "").split(".")[0]: r for r in rows}


# ------------------------------------------------------ 日线层探测与状态落盘

_FB_MAX_CODES = 20        # 备源单日上限（验证集 ≤4 只，上限防误用触发批量外呼）
_PROBE_CACHE = {}         # date8 -> (该日行数, 库内最新 bar)；进程内缓存
_LAST_STATUS = {}         # 最近一次写盘的状态（验证 note 读取，spec_validate.layer_status）


def _layer_probe(date8):
    """T 日日线层探测：{该日行数（0=整层缺失）, 库内最新 bar}；查询失败取 None。"""
    if date8 not in _PROBE_CACHE:
        iso = _date_iso(date8)
        n, last = None, None
        try:
            rows = _db_export(
                "SELECT (SELECT COUNT(*) FROM v_daily_qfq WHERE date = DATE '"
                + iso + "') AS n, (SELECT MAX(date) FROM v_daily_qfq) AS last_bar",
                f"layerprobe_{date8}")
            r = (rows or [{}])[0]
            n = int(r.get("n") or 0)
            last = str(r.get("last_bar") or "")[:10] or None
        except Exception:  # noqa: BLE001 - 探测失败不阻断验证
            pass
        _PROBE_CACHE[date8] = (n, last)
    return _PROBE_CACHE[date8]


def _record_layer_status(date8, layer_n, last_bar, fallback, db_error, note):
    """状态进程内留底 + 原子落盘（server /api/health 的 duckdb 段只读该文件）。"""
    st = {"updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "probed": str(date8),
          "ok": not (layer_n == 0 or db_error),
          "layer_n": layer_n, "last_bar": last_bar,
          "fallback": fallback, "db_error": db_error, "note": note}
    _LAST_STATUS.clear()
    _LAST_STATUS.update(st)
    try:
        fsutil.save_json_atomic(DUCKDB_STATUS, st, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - 状态写盘失败不影响验证
        pass


def layer_status():
    """最近一次 ohlc_rets 的研究库层状态（验证 note / 健康聚合读取）。"""
    return dict(_LAST_STATUS)
