# -*- coding: utf-8 -*-
"""复盘快照全模块历史回补（同花顺数据优先）。

背景：2026-08-28 的 backfill.py 仅回补了 6 个核心模块；本脚本为剩余模块补齐历史：
    market_indices  东财指数日K（失败退新浪 akshare，无成交额）
    breadth         hithink 本地 DuckDB 日线（涨跌家数/9区间/两市成交额）
                    + 快照已有池子推导（涨停/跌停/连板/炸板/真实涨停）
                    + 东财指数日K 大小盘（沪深300 vs 中证1000）
    concepts        hithink index.history（390 概念指数全区间，按天切片）
    extra           DuckDB 涨跌幅分布 + 成交额 TOP20（名称来自 symbol.list）
    hot_stock       hithink special hot-stock-history（按日期历史榜单）
    etf             hithink fund.history（10 只主要 ETF 区间日线）
    global_market   akshare 新浪美股/港股指数日K + 东财日韩指数日K（尽力而为）
    prev.today      DuckDB 日线匹配昨日涨停的当日涨跌幅（晋级率）

用法：
    python backend/backfill_full.py --from 20260824          # 只补一周
    python backend/backfill_full.py                          # 全部缺失快照
    python backend/backfill_full.py --dry-run                # 只列目标
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ht
import em_common  # 东财直连共享封装（backend/em_common.py）
import fsutil     # 原子写盘单一来源（backend/fsutil.py）
import providers  # 复用 _get_concepts / ETF_LIST / _round2 / PCT_BINS

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "recap")
ST_DIR = os.path.join(PROJECT_ROOT, ".status")

MISSING_MODULES = ["market_indices", "breadth", "concepts",
                   "extra", "hot_stock", "etf", "global_market"]

# 东财直连（EM_HOSTS/EM_HEADERS/em_kline）2026-09-04 收敛到 backend/em_common.py
em_kline = em_common.em_kline_rows

INDEX_SECIDS = [  # (secid, 新浪风格代码, 名称)
    ("1.000001", "sh000001", "上证指数"),
    ("0.399001", "sz399001", "深证成指"),
    ("0.399006", "sz399006", "创业板指"),
    ("1.000688", "sh000688", "科创50"),
    ("1.000300", "sh000300", "沪深300"),
    ("1.000852", "sh000852", "中证1000"),
]
INDEX_ROWS = [r for r in INDEX_SECIDS[:5]]  # market_indices 只展示前 5

# 涨跌幅 9 区间单一来源：providers.PCT_BINS（此前本地复制且 0~1% 下界与 providers 不一致）
BINS = providers.PCT_BINS


def d8(date_s):
    return date_s.replace("-", "")


def dms(date_s):
    return ht.date_ms(d8(date_s))


# ---------------------------------------------------------------- DuckDB 部分

def duckdb_rows(start_date="2026-01-05"):
    """本地 DuckDB 导出行级明细（date, thscode, close, amount, pct）。

    注意：hithink db CLI 的 SQL 校验器对词 'ticker' 与 WITH+JOIN 组合会误判，
    故只用单层 JOIN 的导出语句，宽度/分布/TOP20/晋级率全部在 Python 端聚合。
    base 取 start_date 前 10 天，保证首个目标日能取到昨收（lag）。
    """
    base = (datetime.datetime.strptime(start_date, "%Y-%m-%d")
            - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    sql = ("SELECT n.date, n.thscode, n.close, n.amount, "
           "round(100.0*(n.close/q.pcp-1), 2) AS pct "
           "FROM v_daily n JOIN (SELECT thscode, date, "
           "lag(close) OVER (PARTITION BY thscode ORDER BY date) AS pcp "
           "FROM v_daily_qfq WHERE date >= DATE '" + base + "') q "
           "ON n.thscode = q.thscode AND n.date = q.date "
           "WHERE n.date >= DATE '" + start_date + "' "
           "AND q.pcp IS NOT NULL AND n.close > 0")
    out_csv = os.path.join(DATA_DIR, "panel", "pct_history.csv")
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    ht.ht("db", "export", "--sql", sql, "--output", out_csv,
          "--file-format", "csv", timeout=600)

    import csv
    daily_map, top20, pct_map = {}, {}, {}
    with open(out_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            date = str(r["date"]).replace("-", "")
            code = str(r["thscode"])
            ticker = code.split(".")[0]
            try:
                pct = round(float(r["pct"]), 2)
                amount = float(r["amount"] or 0)
                close = float(r["close"])
            except (TypeError, ValueError):
                continue
            d = daily_map.setdefault(date, {
                "up": 0, "down": 0, "flat": 0, "sh_sz_amount": 0.0,
                "b1": 0, "b2": 0, "b3": 0, "b4": 0, "b5": 0,
                "b6": 0, "b7": 0, "b8": 0, "b9": 0})
            if pct > 0:
                d["up"] += 1
            elif pct < 0:
                d["down"] += 1
            else:
                d["flat"] += 1
            for (label, lo, hi), key in zip(
                    BINS, ["b1", "b2", "b3", "b4", "b5",
                           "b6", "b7", "b8", "b9"]):
                if lo == hi:
                    if pct == 0:
                        d[key] += 1
                elif lo <= pct < hi:
                    d[key] += 1
            if code.endswith((".SH", ".SZ")):
                d["sh_sz_amount"] += amount
            lst = top20.setdefault(date, [])
            if len(lst) < 20:
                lst.append({"thscode": code, "close": close,
                            "amount": amount, "pct": pct})
                lst.sort(key=lambda x: -x["amount"])
            elif amount > lst[-1]["amount"]:
                lst[-1] = {"thscode": code, "close": close,
                           "amount": amount, "pct": pct}
                lst.sort(key=lambda x: -x["amount"])
            pct_map[(date, ticker)] = pct
    return daily_map, top20, pct_map


# ---------------------------------------------------------------- 各模块构建

def build_market_indices(idx_series, date):
    rows = []
    for _secid, code, name in INDEX_ROWS:
        ser = idx_series.get(code)
        if not ser or "dates" not in ser:
            continue
        i = ser["dates"].index(date) if date in ser["dates"] else None
        if i is None or i == 0:
            continue
        prev_c = ser["closes"][i - 1]
        c = ser["closes"][i]
        rows.append({
            "代码": code, "名称": name,
            "最新价": providers._round2(c),
            "涨跌额": providers._round2(c - prev_c),
            "涨跌幅": providers._round2((c - prev_c) / prev_c * 100.0),
            "昨收": providers._round2(prev_c),
            "今开": providers._round2(ser["opens"][i]),
            "最高": providers._round2(ser["highs"][i]),
            "最低": providers._round2(ser["lows"][i]),
            "成交额": ser["amounts"][i] if ser["amounts"][i] else None,
        })
    return {"status": "ok", "data": rows} if rows else \
        {"status": "error", "error": "ValueError: 指数切片为空"}


def build_breadth(snap, date, daily_map, prev_amount, idx_series):
    data = {}
    zt = (snap["modules"].get("limit_up_pool") or {}).get("data") or []
    dt = (snap["modules"].get("limit_down_pool") or {}).get("data") or []
    zb = (snap["modules"].get("limit_break_pool") or {}).get("data") or []
    d = daily_map.get(date) or {}
    data.update({
        "上涨": d.get("up"), "下跌": d.get("down"), "平盘": d.get("flat"),
        "涨停家数": len(zt), "跌停家数": len(dt),
        "最高连板": max((r.get("连板数") or 0) for r in zt) if zt else 0,
        "炸板家数": len(zb),
        "真实涨停": sum(1 for r in zt if not r.get("is_st") and not r.get("is_new")),
    })
    if d.get("sh_sz_amount"):
        data["两市成交额"] = float(d["sh_sz_amount"])
    if prev_amount:
        data["昨日两市成交额"] = prev_amount
    hs = (idx_series.get("sh000300") or {}).get("dates") or []
    zz = (idx_series.get("sh000852") or {}).get("dates") or []
    if date in hs and hs.index(date) > 0:
        i = hs.index(date)
        s300 = idx_series["sh000300"]
        p = s300["closes"][i - 1]
        data.setdefault("大小盘", {})["大盘"] = {
            "名称": "沪深300", "涨跌幅": providers._round2(
                (s300["closes"][i] - p) / p * 100.0)}
    if date in zz and zz.index(date) > 0:
        i = zz.index(date)
        s1000 = idx_series["sh000852"]
        p = s1000["closes"][i - 1]
        data.setdefault("大小盘", {})["小盘"] = {
            "名称": "中证1000", "涨跌幅": providers._round2(
                (s1000["closes"][i] - p) / p * 100.0)}
    if "大小盘" in data and "大盘" not in data["大小盘"]:
        data.pop("大小盘")   # 指数源缺失时降级为无大小盘对比
    return {"status": "ok", "data": data}


def build_extra(daily_map, date, top20, name_map):
    d = daily_map.get(date) or {}
    if not d:
        return {"status": "error", "error": "ValueError: 无日线聚合"}
    distribution = []
    for (label, _lo, _hi), key in zip(BINS, ["b1", "b2", "b3", "b4", "b5",
                                             "b6", "b7", "b8", "b9"]):
        distribution.append({"区间": label, "家数": int(d.get(key) or 0)})
    popular = []
    for r in top20.get(date) or []:
        ticker = str(r["thscode"]).split(".")[0]
        popular.append({
            "代码": ticker,
            "名称": name_map.get(ticker) or ticker,
            "最新价": providers._round2(r["close"]),
            "涨跌幅": providers._round2(r["pct"]),
            "成交额": float(r["amount"]),
            "市盈率TTM": None, "市净率": None, "ROE": None, "净利同比": None,
        })
    return {"status": "ok", "data": {
        "distribution": distribution,
        "summary": {"涨": int(d.get("up") or 0), "平": int(d.get("flat") or 0),
                    "跌": int(d.get("down") or 0)},
        "popular": popular,
    }}


def build_hot_stock(rows):
    data = [{"排名": r.get("rank"), "代码": str(r.get("ticker") or ""),
             "名称": r.get("name"), "热度": None, "排名变动": None,
             "趋势": None} for r in rows]
    data.sort(key=lambda x: x["排名"] if x["排名"] is not None else 999)
    return {"status": "ok", "data": data}


def main():
    ap = argparse.ArgumentParser(description="复盘快照全模块历史回补")
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 1. 目标快照
    targets = []
    for fn in sorted(os.listdir(DATA_DIR)):
        if not (len(fn) == 13 and fn.endswith(".json")):
            continue
        date = fn[:8]
        if args.date_from and date < args.date_from:
            continue
        if args.date_to and date > args.date_to:
            continue
        with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
            snap = json.load(f)
        mods = snap.get("modules") or {}
        miss = [m for m in MISSING_MODULES
                if m not in mods or mods[m].get("status") != "ok"]
        if miss:
            targets.append((date, snap, miss))
    print(f"目标快照: {len(targets)} 天"
          + (f"（{targets[0][0]} ~ {targets[-1][0]}）" if targets else ""))
    if args.dry_run or not targets:
        for date, _snap, miss in targets[:8]:
            print(" ", date, "缺:", ",".join(miss))
        return

    # 2. DuckDB 聚合 + TOP20 + pct 映射
    print("[1/6] hithink 本地 DuckDB 聚合（宽度/分布/TOP20）...")
    start_iso = datetime.datetime.strptime(
        targets[0][0], "%Y%m%d").strftime("%Y-%m-%d")
    daily_map, top20, pct_map = duckdb_rows(start_date=start_iso)
    print(f"    天数={len(daily_map)}, TOP20 天数={len(top20)}")


    # 3. 名称表
    print("[3/6] symbol.list 名称表...")
    name_map = ht.symbol_names(cache_dir=providers._cache_dir())

    # 4. concepts：index.history 全区间按天切片
    print("[4/6] hithink index.history 概念区间（390）...")
    concepts = _build_concepts_by_date(targets[0][0], targets[-1][0])

    # 5. etf：fund.history
    print("[5/6] hithink fund.history ETF...")
    etf_by_date = _build_etf_by_date(targets[0][0], targets[-1][0])

    # 6. hot-stock-history + 指数 + 外围
    print("[6/6] 热股榜历史 / 指数日K / 外围...")
    hs_by_date = _build_hot_stock_history([d for d, _s, _m in targets])
    idx_series = _build_index_series()
    global_by_date = _build_global_by_date(targets[0][0], targets[-1][0])

    # 7. 合并写回
    dates_sorted = sorted(daily_map)
    written = 0
    for date, snap, miss in targets:
        mods = snap["modules"]
        if "market_indices" in miss:
            mods["market_indices"] = build_market_indices(idx_series, date)
        if "breadth" in miss:
            idx = dates_sorted.index(datetime.datetime.strptime(
                date, "%Y%m%d").strftime("%Y-%m-%d")) if \
                datetime.datetime.strptime(date, "%Y%m%d").strftime(
                    "%Y-%m-%d") in dates_sorted else None
            prev_amount = (float(daily_map[dates_sorted[idx - 1]]
                                 ["sh_sz_amount"])
                           if idx and idx > 0 else None)
            mods["breadth"] = build_breadth(snap, date, daily_map,
                                            prev_amount, idx_series)
        if "concepts" in miss and date in concepts:
            mods["concepts"] = concepts[date]
        if "extra" in miss:
            mods["extra"] = build_extra(daily_map, date, top20, name_map)
        if "hot_stock" in miss and date in hs_by_date:
            mods["hot_stock"] = build_hot_stock(hs_by_date[date])
        if "etf" in miss and date in etf_by_date:
            mods["etf"] = etf_by_date[date]
        if "global_market" in miss and date in global_by_date:
            mods["global_market"] = global_by_date[date]
        # prev.today（晋级率）
        prev = snap.get("prev") or {}
        if prev.get("zt_codes") and not prev.get("today"):
            rows = [{"代码": c, "名称": "",
                     "涨跌幅": pct_map.get((date, str(c)))}
                    for c in prev["zt_codes"]
                    if pct_map.get((date, str(c))) is not None]
            if rows:
                prev["today"] = {"status": "ok", "data": rows}

        fsutil.save_json_atomic(os.path.join(DATA_DIR, date + ".json"), snap)
        written += 1
        if written % 20 == 0:
            print(f"    已写 {written}/{len(targets)}")
    print(f"完成：写回 {written} 天快照 -> {DATA_DIR}")


def _build_concepts_by_date(d_from, d_to):
    """index.history：390 概念一次拉全区间，按天切片出当日榜。"""
    cat = providers._get_concepts()
    start_ms, end_ms = dms(d_from) - 5 * 86400 * 1000, dms(d_to) + 86400 * 1000

    def fetch(pair):
        code, name = pair
        try:
            d = ht.ht("index", "history", "--thscode", code,
                      "--start-ms", str(start_ms), "--end-ms", str(end_ms),
                      timeout=90)
            return code, name, (d.get("item") or [])
        except Exception:  # noqa: BLE001
            return code, name, []

    ser = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, name, rows in ex.map(fetch, cat):
            lookup = {}   # 8 位日期 -> (close, prev_close, turnover)
            for i, h in enumerate(rows):
                dstr = d8(datetime.datetime.fromtimestamp(
                    h["date_ms"] / 1000).strftime("%Y-%m-%d"))
                prev_c = rows[i - 1]["close_price"] if i > 0 else None
                lookup[dstr] = (h.get("close_price"), prev_c,
                                h.get("turnover"))
            ser[code] = (name, lookup)

    by_date = {}
    for date in _trading_dates(d_from, d_to):
        rows = []
        for code, (name, lookup) in ser.items():
            hit = lookup.get(date)
            if not hit or not hit[1]:
                continue
            c, p, turnover = hit
            rows.append({"概念": name,
                         "涨跌幅": providers._round2((c - p) / p * 100.0),
                         "成交额(亿)": providers._round2((turnover or 0) / 1e8)})
        if not rows:
            continue
        rows.sort(key=lambda x: x["涨跌幅"] if x["涨跌幅"] is not None else -999,
                  reverse=True)
        for i, r in enumerate(rows, 1):
            r["涨幅排名"] = i
        by_date[date] = {"status": "ok", "data": rows}
    return by_date


def _build_etf_by_date(d_from, d_to):
    start_ms, end_ms = dms(d_from) - 5 * 86400 * 1000, dms(d_to) + 86400 * 1000

    def fetch(pair):
        code, name = pair
        try:
            d = ht.ht("fund", "history", "--thscode", code,
                      "--start-ms", str(start_ms), "--end-ms", str(end_ms),
                      timeout=60)
            return code, name, (d.get("item") or [])
        except Exception:  # noqa: BLE001
            return code, name, []

    ser = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for code, name, rows in ex.map(fetch, providers.ETF_LIST):
            lookup = {}
            for i, h in enumerate(rows):
                dstr = d8(datetime.datetime.fromtimestamp(
                    h["date_ms"] / 1000).strftime("%Y-%m-%d"))
                prev_c = rows[i - 1]["close_price"] if i > 0 else None
                lookup[dstr] = (h.get("close_price"), prev_c,
                                h.get("turnover"))
            ser[code] = (name, lookup)

    by_date = {}
    for date in _trading_dates(d_from, d_to):
        rows = []
        for code, (name, lookup) in ser.items():
            hit = lookup.get(date)
            if not hit or not hit[1]:
                continue
            c, p, turnover = hit
            rows.append({
                "代码": code.split(".")[0], "名称": name,
                "最新价": providers._round2(c),
                "涨跌幅": providers._round2((c - p) / p * 100.0),
                "成交额(亿)": providers._round2((turnover or 0) / 1e8),
                "换手率": None,
            })
        if rows:
            by_date[date] = {"status": "ok", "data": rows}
    return by_date


def _build_hot_stock_history(dates):
    def fetch(d):
        try:
            ds = f"{d[:4]}-{d[4:6]}-{d[6:]}"   # 接口要求 YYYY-MM-DD
            r = ht.ht("special", "hot-stock-history", "--date", ds, timeout=60)
            return d, (r.get("item") or [])
        except Exception:  # noqa: BLE001
            return d, []

    by_date = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for d, rows in ex.map(fetch, dates):
            if rows:
                by_date[d] = rows
    return by_date


def _build_index_series():
    """东财日K -> {新浪码: {dates, closes, opens, highs, lows, amounts}}。

    东财限流时退新浪 akshare（stock_zh_index_daily，无成交额）。
    """
    out = {}
    for secid, code, _name in INDEX_SECIDS:
        rows = em_kline(secid, 101, 185)
        if rows:
            out[code] = {"dates": [d8(r["date"]) for r in rows],
                         "closes": [r["close"] for r in rows],
                         "opens": [r["open"] for r in rows],
                         "highs": [r["high"] for r in rows],
                         "lows": [r["low"] for r in rows],
                         "amounts": [r["amount"] for r in rows]}
        else:
            try:
                import akshare as ak
                df = ak.stock_zh_index_daily(symbol=code)
                out[code] = {
                    "dates": [d8(str(d)[:10]) for d in df["date"]],
                    "closes": [float(c) for c in df["close"]],
                    "opens": [float(c) for c in df["open"]],
                    "highs": [float(c) for c in df["high"]],
                    "lows": [float(c) for c in df["low"]],
                    "amounts": [None] * len(df)}
                print(f"    {code}: 东财限流，已用新浪日K兜底（无成交额）")
            except Exception as e:  # noqa: BLE001
                print(f"    {code}: 指数源失败 {type(e).__name__}")
        time.sleep(1)
    return out


def _build_global_by_date(d_from, d_to):
    """美股/港股（新浪 akshare）+ 日韩（东财，尽力而为）按天切片。"""
    import akshare as ak

    def pct_series(df):
        dates = [d8(str(d)[:10]) for d in df["date"]]
        closes = [float(c) for c in df["close"]]
        return dates, closes

    series = []
    for sym, name in [(".DJI", "道琼斯"), (".IXIC", "纳斯达克"), (".INX", "标普500")]:
        try:
            df = ak.index_us_stock_sina(symbol=sym)
            dates, closes = pct_series(df)
            series.append((dates, closes, name))
        except Exception:  # noqa: BLE001
            pass
    for sym, name in [("HSI", "恒生指数"), ("HSTECH", "恒生科技指数")]:
        try:
            df = ak.stock_hk_index_daily_sina(symbol=sym)
            dates, closes = pct_series(df)
            series.append((dates, closes, name))
        except Exception:  # noqa: BLE001
            pass
    jp_kr = []
    for secid, name in [("100.N225", "日经225"), ("100.KS11", "韩国KOSPI")]:
        rows = em_kline(secid, 101, 185, tries=2)
        if rows:
            for r in rows:
                r["date"] = d8(r["date"])
            jp_kr.append((rows, name))

    by_date = {}
    for date in _trading_dates(d_from, d_to):
        us, hk, jk = [], [], []
        for rows, name in jp_kr:
            past = [r for r in rows if r["date"] <= date]
            if len(past) >= 2:
                c, p = past[-1]["close"], past[-2]["close"]
                jk.append({"名称": name, "最新价": providers._round2(c),
                           "涨跌幅": providers._round2((c - p) / p * 100.0)})
        for dates, closes, name in series:
            past = [i for i, d0 in enumerate(dates) if d0 <= date]
            if not past:
                continue
            i = past[-1]
            if i == 0:
                continue
            c, p = closes[i], closes[i - 1]
            row = {"名称": name, "最新价": providers._round2(c),
                   "涨跌幅": providers._round2((c - p) / p * 100.0)}
            if name in ("道琼斯", "纳斯达克", "标普500"):
                row["日期"] = dates[i]
                us.append(row)
            else:
                hk.append(row)
        by_date[date] = {"status": "ok",
                         "data": {"日韩": jk, "港股": hk, "美股": us}}
    return by_date


def _trading_dates(d_from, d_to):
    """闭区间内的交易日（hithink 日历），统一返回 8 位日期。"""
    try:
        cal = ht.ht("market", "calendar")
        all_dates = sorted(str(x.get("date")) for x in (cal.get("item") or []))
        return [d for d in all_dates if d8(d_from) <= d <= d8(d_to)]
    except Exception:  # noqa: BLE001
        out, cur = [], datetime.datetime.strptime(d8(d_from), "%Y%m%d")
        end = datetime.datetime.strptime(d8(d_to), "%Y%m%d")
        while cur <= end:
            if cur.weekday() < 5:
                out.append(cur.strftime("%Y%m%d"))
            cur += datetime.timedelta(days=1)
        return out


if __name__ == "__main__":
    main()
