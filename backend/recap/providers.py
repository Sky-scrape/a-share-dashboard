# -*- coding: utf-8 -*-
"""A股盘后数据抓取模块（providers）。

13 个模块函数，各管一摊，互相独立：
market_indices / breadth / limit_up_pool / limit_down_pool / limit_break_pool /
boards / concepts / extra / hot_stock / etf / global_market / regulatory / lhb

数据源（2026-08-28 起）：主力数据源切换为 hithink-finance CLI（同花顺口径，
封装见 ht.py）——涨停/跌停/炸板池、龙虎榜、热股榜、行业/概念指数、全市场快照、
ETF 快照、估值；akshare（新浪/东财）保留用于大盘指数、两市成交额、大小盘、
监管公告与外围市场。每个函数返回 {"status": "ok", "data": ...} 或
{"status": "error", "error": "真实异常类型: 信息"}。
一个模块失败不连坐其他模块；每个模块失败自动重试 2 次（共 3 次尝试，间隔 3 秒）。
"""
import sys
import time
import json
import datetime as _dt
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# 禁用系统代理：直连东财/新浪接口（坏代理会导致请求挂起重试）
import os
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

try:
    import akshare as ak
except ImportError:  # 可选依赖：缺失时模块仍可导入，用到新浪/东财通道的模块单独降级报错
    ak = None
import requests

import ht
import http_retry

# 2026-09-04 起不再在模块级 reconfigure stdout：本模块会被宿主进程 import
# （smoke/server 链路），导入即改写宿主输出编码是 auc_collector 踩过的坑。
# 作为脚本直跑时由调用方（fetch_daily/backfill/backfill_full）自行 reconfigure。

# 当前抓取日期，由 set_context 设置（fetch_daily.py / backfill.py 调用），格式 8 位 "20260805"。
# 2026-09-04 收口：此前允许外部直接改写 providers.DATE/HISTORICAL，5 个按 DATE 键的
# 缓存隐性依赖这个全局正确；现在统一走 set_context，谁改写、何时改写一目了然。
DATE = ""

# 历史补抓模式：DATE 非当天时为 True，快照当日接口（成交额补齐等）自动降级
HISTORICAL = False


def set_context(date, historical=None):
    """设置本次抓取的日期上下文（外部唯一改写入口）。

    date: 8 位 "20260805"；historical: DATE 非当天时 True，None=自动推断。"""
    global DATE, HISTORICAL
    DATE = str(date or "")
    if historical is None:
        historical = DATE != time.strftime("%Y%m%d")
    HISTORICAL = bool(historical)


def require_ak():
    """akshare 缺失时给出可行动的报错（而不是 AttributeError: NoneType）。"""
    if ak is None:
        raise RuntimeError("akshare 未安装（pip install akshare），该模块依赖新浪/东财通道")
    return ak


# 模块清单单一来源：backend/recap/modules.py（新增/删除模块只改那里）
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from modules import MODULES  # noqa: E402

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

# 全市场快照缓存：breadth/extra/晋级率共用一次抓取（hithink market.snapshot）
_MKT_SNAP_CACHE = {"date": None, "rows": None, "by_code": None}


def _date_cached(cache, fetch):
    """按 DATE 键的取数缓存：命中返回行，未命中 fetch() 并写入。失败抛异常。"""
    if cache.get("date") == DATE and cache.get("rows") is not None:
        return cache["rows"]
    rows = fetch()
    cache.update(date=DATE, rows=rows)
    return rows


def _round2(x):
    """安全保留两位小数（None 透传）。"""
    return None if x is None else round(float(x), 2)


def _cache_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ht_cache")


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
        ["special", "limit-up-pool", "--date-ms", str(ht.date_ms(DATE))]))


def _get_dt_pool_ht():
    """hithink 跌停池（带缓存）。失败抛异常。"""
    return _date_cached(_HT_DT_CACHE, lambda: ht.pool_all(
        ["special", "limit-down-pool", "--date-ms", str(ht.date_ms(DATE))]))


def _get_zb_pool_ht():
    """hithink 炸板池（带缓存）。失败抛异常。"""
    return _date_cached(_HT_ZB_CACHE, lambda: ht.pool_all(
        ["special", "limit-break-pool", "--date-ms", str(ht.date_ms(DATE))]))


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
    缺失/过旧才退回本模块按成分自建并日落缓存（首次切口径时两边同日重建，
    内容同源同名，不会出现两套行业名）。"""
    try:
        import industry_common
    except ImportError:  # 单独被 import 时 backend 根可能不在 sys.path
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import industry_common
    shared = industry_common.load_shared_ticker_map()
    if shared:
        return shared
    cache_file = os.path.join(_cache_dir(), f"stock_industry_{DATE}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    industries = _get_industries()
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
    os.makedirs(_cache_dir(), exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
    return mapping


def _get_concepts():
    """同花顺概念目录 [(thscode, name)]（本地缓存 7 天）。"""
    items = ht.catalog("cn_concept", cache_dir=_cache_dir())
    out = [(str(x["thscode"]), x["name"]) for x in items]
    if not out:
        raise RuntimeError("ValueError: 概念目录为空")
    return out


def _symbol_name_map():
    """symbol.list 全量 ticker->name（当日缓存）；估值接口失败时的名称兜底。"""
    return ht.symbol_names(cache_dir=_cache_dir())


def _current_report():
    """按当前日期推导最新已披露报告期（financials 用）。"""
    t = datetime.now()
    if t.month <= 4:
        return f"{t.year - 1}-4"   # 上一年年报
    if t.month <= 8:
        return f"{t.year}-1"       # 一季报
    if t.month <= 10:
        return f"{t.year}-2"       # 半年报
    return f"{t.year}-3"           # 三季报


def _financial_indicators(thscodes):
    """批量查最新报告期财务指标：{thscode: (加权ROE, 净利同比)}。单股失败跳过。"""
    report = _current_report()

    def fetch(ths):
        try:
            fd = ht.ht("financials", "indicators", "--thscode", ths,
                       "--report", report, timeout=30)
            roe = yoy = None
            for ab in fd.get("abilities") or []:
                for ind in ab.get("indicators") or []:
                    iid = ind.get("index_id")
                    v = ind.get("value")
                    try:
                        fv = float(v) if v not in (None, "", "-") else None
                    except (TypeError, ValueError):
                        fv = None
                    if iid == "index_weighted_avg_roe":
                        roe = fv
                    elif iid == ("calculate_parent_holder_net_profit_"
                                 "yoy_growth_ratio"):
                        yoy = fv
            return ths, roe, yoy
        except Exception:  # noqa: BLE001 - 单股财务失败不影响行情
            return ths, None, None

    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for ths, roe, yoy in ex.map(fetch, thscodes):
            out[ths] = (roe, yoy)
    return out


def prev_zt_today(prev_codes):
    """昨日涨停代码 -> 今日涨跌幅（晋级率计算原料）。

    入参：昨日涨停代码列表（str）。
    返回：{"status": "ok", "data": [{"代码", "涨跌幅"}...]} 或 error。
    数据源：hithink 全市场快照本地匹配（复用缓存，无额外网络请求）。
    """
    try:
        snap = _snap_by_code()
        rows = []
        for c in (prev_codes or []):
            code = _norm_code(c)
            s = snap.get(code)
            if s is None:
                continue
            rows.append({"代码": code, "名称": "",
                         "涨跌幅": _round2(s.get("price_change_ratio_pct"))})
        return {"status": "ok", "data": rows}
    except Exception as e:
        return _err(e)


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


# ---------------------------------------------------------------- 模块实现

def market_indices():
    """大盘指数：上证/深成/创业板/科创50/沪深300 最新价、涨跌幅、成交额。"""
    try:
        df = _retry(lambda: ak.stock_zh_index_spot_sina())
        targets = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000688": "科创50",
            "sh000300": "沪深300",
        }
        data = []
        for _, row in df.iterrows():
            code = str(row["代码"])
            if code in targets:
                data.append({
                    "代码": code,
                    "名称": targets[code],
                    "最新价": _native(row["最新价"]),
                    "涨跌额": _native(row["涨跌额"]),
                    "涨跌幅": _native(row["涨跌幅"]),
                    "昨收": _native(row["昨收"]),
                    "今开": _native(row["今开"]),
                    "最高": _native(row["最高"]),
                    "最低": _native(row["最低"]),
                    "成交额": _native(row["成交额"]),
                })
        if not data:
            return {"status": "error", "error": "ValueError: 指数过滤结果为空"}
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def breadth():
    """市场宽度：涨跌家数（全市场快照）+ 涨停/跌停/炸板/连板（hithink 池子）。

    两市成交额与大小盘继续用新浪指数 spot（与历史快照口径一致）。
    """
    try:
        rows = _get_market_snapshot()
        pcts = [r.get("price_change_ratio_pct") for r in rows
                if r.get("price_change_ratio_pct") is not None]
        up_n = sum(1 for p in pcts if p > 0)
        down_n = sum(1 for p in pcts if p < 0)
        flat_n = len(pcts) - up_n - down_n

        zt = _get_zt_pool_ht()
        dt_rows = _get_dt_pool_ht()
        zb = _get_zb_pool_ht()
        max_lb = max((r.get("continue_day_cnt") or 1) for r in zt) if zt else 0
        real_zt = sum(1 for r in zt
                      if not r.get("is_st") and not r.get("is_new"))

        data = {
            "上涨": up_n, "下跌": down_n, "平盘": flat_n,
            "涨停家数": len(zt), "跌停家数": len(dt_rows),
            "最高连板": max_lb, "炸板家数": len(zb),
            "真实涨停": real_zt,
        }

        # 两市成交额（沪市 sh000001 + 深市 sz399106）+ 大小盘对比（沪深300 / 中证1000）
        try:
            spot = _retry(lambda: ak.stock_zh_index_spot_sina())
            spot_map = {str(r["代码"]): r for _, r in spot.iterrows()}
            sh = spot_map.get("sh000001")
            sz = spot_map.get("sz399106")
            if sh is not None and sz is not None:
                data["两市成交额"] = _native(float(sh["成交额"]) + float(sz["成交额"]))
            hs = spot_map.get("sh000300")
            zz = spot_map.get("sh000852")
            if hs is not None and zz is not None:
                data["大小盘"] = {
                    "大盘": {"名称": "沪深300", "涨跌幅": _native(hs["涨跌幅"])},
                    "小盘": {"名称": "中证1000", "涨跌幅": _native(zz["涨跌幅"])},
                }
        except Exception:  # noqa: BLE001 - 指数补充失败不影响情绪主数据
            pass

        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def limit_up_pool():
    """涨停池：hithink（同花顺口径），含连板数/封板时间/封板资金/涨停原因。

    成交额由全市场快照按代码补齐（历史日期快照不可用则留空）；
    所属行业由 index.constituents 映射补齐。
    """
    try:
        zt = _get_zt_pool_ht()
        snap = {} if HISTORICAL else _snap_by_code()
        ind_map = _get_industry_map()
        data = []
        for r in zt:
            code = str(r.get("ticker") or "")
            s = snap.get(code) or {}
            data.append({
                "代码": code, "名称": r.get("name"),
                "涨跌幅": _round2(r.get("price_change_ratio_pct")),
                "最新价": r.get("last_price"),
                "连板数": r.get("continue_day_cnt"),
                "连板文本": r.get("continue_day_text"),
                "首次封板时间": r.get("limit_up_time"),
                "最后封板时间": r.get("limit_up_time"),
                "封板资金": r.get("seal_money"),
                "最大封板资金": r.get("max_seal_money"),
                "涨停原因": r.get("limit_up_reason"),
                "成交额": s.get("turnover"),
                "所属行业": ind_map.get(code, ""),
                "is_st": bool(r.get("is_st")), "is_new": bool(r.get("is_new")),
            })
        data.sort(key=lambda x: (x["连板数"] or 0), reverse=True)
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def limit_down_pool():
    """跌停池：hithink（同花顺口径），含封板时间/换手率；成交额由快照补齐。"""
    try:
        rows = _get_dt_pool_ht()
        snap = {} if HISTORICAL else _snap_by_code()
        ind_map = _get_industry_map()
        data = []
        for r in rows:
            code = str(r.get("ticker") or "")
            s = snap.get(code) or {}
            data.append({
                "代码": code, "名称": r.get("name"),
                "涨跌幅": _round2(r.get("price_change_ratio_pct")),
                "最新价": r.get("last_price"),
                "首次封板时间": r.get("first_limit_time"),
                "最后封板时间": r.get("last_limit_time"),
                "换手率": _round2(r.get("turnover_ratio_pct")),
                "成交额": s.get("turnover"),
                "所属行业": ind_map.get(code, ""),
            })
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def limit_break_pool():
    """炸板池：当日曾涨停但收盘未封住的股票（hithink，含炸板次数/成交额）。"""
    try:
        rows = _get_zb_pool_ht()
        ind_map = _get_industry_map()
        data = [{
            "代码": str(r.get("ticker") or ""), "名称": r.get("name"),
            "涨跌幅": _round2(r.get("price_change_ratio_pct")),
            "最新价": r.get("last_price"),
            "炸板次数": r.get("open_times"),
            "换手率": _round2(r.get("turnover_ratio_pct")),
            "成交额": r.get("turnover"),
            "所属行业": ind_map.get(str(r.get("ticker") or ""), ""),
        } for r in rows]
        data.sort(key=lambda x: (x["涨跌幅"] if x["涨跌幅"] is not None else -999),
                  reverse=True)
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def boards():
    """行业板块：同花顺一级行业 90 个（hithink index）。

    近 5 日 = index.history（含当天，彻底解决滞后 1 天问题）；
    当日涨跌幅优先取自历史日线当天（支持历史日期回补），index.snapshot 仅兑底。
    输出结构与旧版一致：{名称, 涨跌幅, history: [{日期, 收盘价, 涨跌幅}]}。
    """
    try:
        industries = _get_industries()
        codes = [c for c, _ in industries]
        snap = ht.index_snapshot_batches(codes)
        end_dt = datetime.strptime(DATE, "%Y%m%d")
        start_s = (end_dt - timedelta(days=10)).strftime("%Y%m%d")
        today_s = end_dt.strftime("%Y-%m-%d")
        start_ms = ht.date_ms(start_s)
        end_ms = ht.date_ms(DATE) + 86400 * 1000  # 含当天

        def fetch_hist(code):
            try:
                d = ht.ht("index", "history", "--thscode", code,
                          "--start-ms", str(start_ms), "--end-ms", str(end_ms),
                          timeout=60)
                return code, (d.get("item") or [])
            except Exception:  # noqa: BLE001 - 单行业历史失败跳过
                return code, []

        with ThreadPoolExecutor(max_workers=8) as ex:
            hists = dict(ex.map(fetch_hist, codes))

        result = []
        for code, name in industries:
            row = snap.get(code)
            if row is None:
                continue
            hist_raw = hists.get(code) or []
            hist = []
            for i, hrow in enumerate(hist_raw):
                close = hrow.get("close_price")
                prev_close = hist_raw[i - 1].get("close_price") if i > 0 else None
                dstr = datetime.fromtimestamp(
                    hrow["date_ms"] / 1000).strftime("%Y-%m-%d")
                hist.append({
                    "日期": dstr, "收盘价": _round2(close),
                    "涨跌幅": None if not prev_close else _round2(
                        (close - prev_close) / prev_close * 100.0),
                })
            # 涨跌幅优先取自历史日线当天（支持历史日期回补），快照仅兑底
            pct = None
            if hist and hist[-1]["日期"] == today_s:
                pct = hist[-1]["涨跌幅"]
            if pct is None:
                pct = _round2(row.get("price_change_ratio_pct"))
            item = {"名称": name, "涨跌幅": pct, "history": hist[-5:]}
            if not hist or hist[-1]["日期"] != today_s:
                # 历史缺当天兜底（与旧版逻辑一致）
                item["history"] = hist[-4:] + [{
                    "日期": today_s, "收盘价": row.get("last_price"), "涨跌幅": pct}]
            result.append(item)
        if not result:
            return {"status": "error", "error": "ValueError: 行业数据为空"}
        result.sort(key=lambda x: x["涨跌幅"], reverse=True)
        return {"status": "ok", "data": result}
    except Exception as e:
        return _err(e)


def regulatory():
    """监管榜单：当日监管相关公告，按四个分类组织。

    - abnormal_wave  股票交易异常波动公告
    - penalty        纪律处分 / 通报批评 / 监管函 / 警示函 / 监管措施 / 立案调查 / 行政处罚
    """
    try:
        df = _retry(lambda: ak.stock_notice_report(symbol="全部", date=DATE))
        df = df.copy()
        df["标题"] = df["公告标题"].astype(str)
        df["类型"] = df["公告类型"].astype(str)

        result = {}
        used = set()

        ab = df[df["类型"].str.contains("异常波动", na=False)]
        result["abnormal_wave"] = _records(ab)
        used.update(ab.index)

        rest = df.drop(index=used)
        pen = rest[rest["标题"].str.contains(
            "纪律处分|通报批评|监管函|警示函|监管措施|立案调查|行政处罚|公开谴责|限制交易",
            regex=True, na=False)]
        result["penalty"] = _records(pen)
        used.update(pen.index)

        return {"status": "ok", "data": result}
    except Exception as e:
        return _err(e)


def concepts():
    """概念板块：同花顺概念指数当日快照（hithink，批量秒级）。

    输出：概念 / 涨跌幅 / 成交额(亿) / 涨幅排名。
    （旧版 info_ths 的资金净流入/涨跌家数同花顺批量接口不再提供，已移除）
    """
    try:
        cat = _get_concepts()
        snap = ht.index_snapshot_batches([c for c, _ in cat])
        rows = []
        for code, name in cat:
            r = snap.get(code)
            if r is None:
                continue
            rows.append({
                "概念": name,
                "涨跌幅": _round2(r.get("price_change_ratio_pct")),
                "成交额(亿)": _round2((r.get("turnover") or 0) / 1e8),
            })
        if not rows:
            return {"status": "error", "error": "ValueError: 概念数据为空"}
        rows.sort(key=lambda x: x["涨跌幅"] if x["涨跌幅"] is not None else -999,
                  reverse=True)
        for i, r in enumerate(rows, 1):
            r["涨幅排名"] = i
        return {"status": "ok", "data": rows}
    except Exception as e:
        return _err(e)


# 涨跌幅区间分布（同花顺口径 9 区间：涨停 → 跌停）。
# 单一来源：backfill_full 的全量回补分布同用这一份（此前两份各自维护，
# 且 0~1% 下界一处 0.01 一处 1e-6，小于 0.01% 的微涨会被漏计）。
PCT_BINS = [
    ("涨停", 9.9, 999), ("涨停-5%", 5, 9.9), ("5-1%", 1, 5),
    ("1-0%", 1e-6, 1), ("平盘", 0, 0), ("0-1%", -1, 0),
    ("1-5%", -5, -1), ("5%-跌停", -9.9, -5), ("跌停", -999, -9.9),
]


def extra():
    """市场补充：涨跌幅区间分布 + 成交额 TOP20（hithink 全市场快照）。

    TOP20 名称/估值用 valuation.snapshot（symbol.list 名称兜底）。
    """
    try:
        rows = _get_market_snapshot()
        live = [r for r in rows
                if r.get("last_price") and r.get("prev_price")
                and r.get("price_change_ratio_pct") is not None]
        pcts = [r["price_change_ratio_pct"] for r in live]

        distribution = []
        for label, lo, hi in PCT_BINS:
            if lo == hi:  # 平盘：精确 0
                n = sum(1 for p in pcts if p == 0)
            else:
                n = sum(1 for p in pcts if lo <= p < hi)
            distribution.append({"区间": label, "家数": n})
        n_sum = sum(x["家数"] for x in distribution)
        if n_sum != len(pcts):
            sys.stderr.write(
                f"[providers] 涨跌幅分布自检失败: 区间和 {n_sum} != 样本 {len(pcts)}\n")
        up_n = sum(1 for p in pcts if p > 0)
        flat_n = sum(1 for p in pcts if p == 0)
        down_n = len(pcts) - up_n - flat_n

        top = sorted(live, key=lambda r: (r.get("turnover") or 0),
                     reverse=True)[:20]
        val_map = {}
        try:
            v = ht.ht("valuation", "snapshot",
                      "--thscodes", ",".join(r["thscode"] for r in top))
            val_map = {x["thscode"]: x for x in (v.get("item") or [])}
        except Exception:  # noqa: BLE001 - 估值失败不阻塞成交额榜
            pass
        name_map = _symbol_name_map() if not val_map else {}
        fin_map = _financial_indicators([r["thscode"] for r in top])

        popular = []
        for r in top:
            v = val_map.get(r["thscode"]) or {}
            roe, yoy = fin_map.get(r["thscode"], (None, None))
            popular.append({
                "代码": str(r.get("ticker") or ""),
                "名称": v.get("name") or name_map.get(str(r.get("ticker")))
                        or str(r.get("ticker") or ""),
                "最新价": r.get("last_price"),
                "涨跌幅": _round2(r.get("price_change_ratio_pct")),
                "成交额": r.get("turnover"),
                "市盈率TTM": _round2(v.get("pe_ttm")),
                "市净率": _round2(v.get("pb_mrq")),
                "ROE": _round2(roe),
                "净利同比": _round2(yoy),
            })
        return {
            "status": "ok",
            "data": {
                "distribution": distribution,
                "summary": {"涨": up_n, "平": flat_n, "跌": down_n},
                "popular": popular,
            },
        }
    except Exception as e:
        return _err(e)


def _get_hot_ht():
    """hithink 热股榜原始行（带缓存）。失败抛异常。"""
    return _date_cached(_HT_HOT_CACHE, lambda: (
        ht.ht("special", "hot-stock", "--period", "day").get("item") or []))


def hot_stock():
    """热股榜：同花顺个股热度 TOP（hithink special，day 周期）。"""
    try:
        rows = _get_hot_ht()
        data = [{
            "排名": r.get("rank"),
            "代码": str(r.get("ticker") or ""),
            "名称": r.get("name"),
            "热度": r.get("heat"),
            "排名变动": r.get("rank_change"),
            "趋势": r.get("rank_trend"),
        } for r in rows]
        data.sort(key=lambda x: x["排名"] if x["排名"] is not None else 999)
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


ETF_LIST = [
    ("510050.SH", "上证50ETF"), ("510300.SH", "沪深300ETF"),
    ("588000.SH", "科创50ETF"), ("159915.SZ", "创业板ETF"),
    ("512880.SH", "证券ETF"), ("512690.SH", "酒ETF"),
    ("512480.SH", "半导体ETF"), ("515790.SH", "光伏ETF"),
    ("516010.SH", "游戏ETF"), ("512170.SH", "医疗ETF"),
]


def etf():
    """ETF 风向：主要宽基/行业 ETF 当日表现（hithink fund.snapshot）。"""
    try:
        def fetch(pair):
            code, name = pair
            try:
                d = ht.ht("fund", "snapshot", "--thscode", code, timeout=30)
                item = (d.get("item") or [{}])[0]
                return {
                    "代码": code.split(".")[0], "名称": name,
                    "最新价": item.get("last_price"),
                    "涨跌幅": _round2(item.get("price_change_ratio_pct")),
                    "成交额(亿)": _round2((item.get("turnover") or 0) / 1e8),
                    "换手率": _round2(item.get("turnover_ratio_pct")),
                }
            except Exception:  # noqa: BLE001 - 单只 ETF 失败跳过
                return None

        with ThreadPoolExecutor(max_workers=5) as ex:
            data = [r for r in ex.map(fetch, ETF_LIST) if r]
        if not data:
            return {"status": "error", "error": "RuntimeError: ETF 快照全部失败"}
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def _em_global_quotes(secids):
    """东财全球指数实时（push2delay 直连）：返回 [{名称, 最新价, 涨跌幅}]。"""
    url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           "&secids=" + ",".join(secids) + "&fields=f2,f3,f4,f12,f14")
    try:
        r = requests.get(url, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://quote.eastmoney.com/"})
        data = r.json()
        diff = (data.get("data") or {}).get("diff") or []
        rows = []
        for it in diff:
            if not it.get("f14"):
                continue
            rows.append({
                "名称": it.get("f14"),
                "最新价": it.get("f2"),
                "涨跌幅": it.get("f3"),
            })
        return rows
    except Exception:  # noqa: BLE001 - 外围失败不阻塞整体
        return []


def global_market():
    """外围参考：日韩（东财实时）+ 港股（新浪）+ 美股三大指数（隔夜收盘）。"""
    try:
        # 日韩：东财直连（push2delay）
        jp_kr = _em_global_quotes(["100.N225", "100.KS11"])

        hk = _retry(lambda: ak.stock_hk_index_spot_sina())
        hk_names = {"恒生指数", "恒生科技指数"}
        hk_rows = []
        for _, r in hk.iterrows():
            if r["名称"] in hk_names:
                hk_rows.append({
                    "名称": r["名称"], "最新价": _native(r["最新价"]),
                    "涨跌幅": _native(r["涨跌幅"]),
                })

        us_rows = []
        for sym, label in [(".DJI", "道琼斯"), (".IXIC", "纳斯达克"), (".INX", "标普500")]:
            try:
                df = _retry(lambda: ak.index_us_stock_sina(symbol=sym))
                closes = df["close"].astype(float).tolist()
                if len(closes) >= 2:
                    pct = round((closes[-1] - closes[-2]) / closes[-2] * 100.0, 2)
                    us_rows.append({
                        "名称": label, "最新价": round(closes[-1], 2),
                        "涨跌幅": pct, "日期": str(df.iloc[-1]["date"])[:10],
                    })
            except Exception:  # noqa: BLE001 - 单个指数失败不影响整体
                pass

        return {"status": "ok", "data": {"日韩": jp_kr, "港股": hk_rows, "美股": us_rows}}
    except Exception as e:
        return _err(e)


def _get_lhb_ht():
    """hithink 龙虎榜原始 stock_items（带缓存）。失败抛异常。"""
    return _date_cached(_HT_LHB_CACHE, lambda: (
        ht.ht("special", "dragon-tiger", "--date",
              f"{DATE[:4]}-{DATE[4:6]}-{DATE[6:]}", timeout=90).get("stock_items") or []))


def lhb():
    """龙虎榜：hithink（含游资/机构净买、概念、涨停原因、热度排名）。"""
    try:
        items = _get_lhb_ht()
        data = []
        for r in items:
            change = r.get("change")
            net_rate = r.get("net_rate")
            concepts = "、".join(c.get("name") for c in (r.get("concept_list") or [])[:3])
            reason = r.get("limit_reason")
            if not reason:
                days = r.get("range_days")
                reason = f"龙虎榜({days}天)" if days else "龙虎榜"
            data.append({
                "代码": str(r.get("ticker") or ""), "名称": r.get("name"),
                "涨跌幅": None if change is None else _round2(change * 100.0),
                "龙虎榜净买额": r.get("net_value"),
                "龙虎榜买入额": r.get("buy_value"),
                "龙虎榜卖出额": r.get("sell_value"),
                "净买额占总成交比": None if net_rate is None else _round2(net_rate * 100.0),
                "游资净买额": r.get("hot_money_net_value"),
                "机构净买额": r.get("org_net_value"),
                "上榜原因": reason,
                "概念": concepts,
                "热度排名": r.get("hot_rank"),
                "上榜天数": r.get("range_days"),
            })
        data.sort(key=lambda x: (x["龙虎榜净买额"] or 0), reverse=True)
        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def speculation():
    """投机分析：题材核心/偏离值雷达/高位承接/情绪周期/异动事件。

    计算引擎在 speculate.py（本地快照 + DuckDB 全市场扫描 + index.history 基准
    + special anomaly-list）；本函数只组装当日 bundles，复用池子/热股/龙虎榜缓存，
    无重复上游调用。历史日期（backfill 的 HISTORICAL 模式）不抓异动榜单，
    由引擎内部按 DATE 是否今天判定。
    """
    try:
        import speculate
        import snapio

        def _rows(res):
            return res.get("data") or [] if res.get("status") == "ok" else []

        bundles = {
            "zt": _rows(limit_up_pool()),
            "zb": _rows(limit_break_pool()),
            "dt": _rows(limit_down_pool()),
            "hot": _rows(hot_stock()),
            "lhb": _rows(lhb()),
            "concepts": _rows(concepts()),   # 概念指数当日快照（备选池概念热度用）
        }
        prev_bundles = None
        prev_dates = [d for d in snapio.list_dates() if d < DATE]
        if prev_dates:
            pm = ((snapio.load(prev_dates[0]) or {}).get("modules") or {}) \
                .get("limit_up_pool") or {}
            prev_bundles = {
                "zt": pm.get("data") or [] if pm.get("status") == "ok" else []}
        return speculate.compute(DATE, bundles, prev_bundles,
                                 historical=HISTORICAL
                                 or DATE != time.strftime("%Y%m%d"))
    except Exception as e:
        return _err(e)


def fetch_all():
    """抓取全部模块，返回 {模块名: 结果}。"""
    return {name: getattr(sys.modules[__name__], name)() for name in MODULES}


if __name__ == "__main__":
    # 简单自检：不设 DATE 时直接跑，用于人工探测
    DATE = time.strftime("%Y%m%d")
    for name, result in fetch_all().items():
        print(json.dumps({name: result}, ensure_ascii=False)[:400])
