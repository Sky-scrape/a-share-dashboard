# -*- coding: utf-8 -*-
"""榜单与投机数据模块（2026-09-27 自 providers.py 拆出，行为零变化）。

收拢热股榜 hot_stock、龙虎榜 lhb、ETF 风向 etf、涨跌分布与成交额 TOP20 extra
（含估值/财务指标配套 _symbol_name_map/_current_report/_financial_indicators）
与投机分析组装 speculation：前四者都是 hithink 榜单/快照通道（按 DATE 缓存门
在 _common），speculation 只组装当日 bundles 喂给 speculate.py 计算引擎，复用
池子/热股/龙虎榜/概念缓存，无重复上游调用——归并为「榜单消费方」一处。
"""
import json
import os
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import fsutil
import ht

import providers as _prov
from ._common import (
    _err, _round2, _date_cached, _cache_dir,
    _HT_HOT_CACHE, _HT_LHB_CACHE,
    _get_market_snapshot, PCT_BINS,
)
from .pools import limit_up_pool, limit_down_pool, limit_break_pool
from .sectors import concepts


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
    """批量查最新报告期财务指标：{thscode: (加权ROE, 净利同比)}。单股失败跳过。

    财务数据季度才变，而 TOP20 每轮全市场刷新都会走到——按 (DATE, 报告期) 做
    .ht_cache 磁盘缓存（首抓后同日零子进程），键含 DATE 防跨日脏读。
    """
    report = _current_report()
    cache_file = os.path.join(_cache_dir(),
                              f"fin_ind_{_prov.DATE}_{report.replace('-', '')}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("report") == report:
                return {k: tuple(v) for k, v in (d.get("map") or {}).items()}
        except Exception:  # noqa: BLE001 - 缓存坏就重抓
            pass

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
    os.makedirs(_cache_dir(), exist_ok=True)
    fsutil.save_json_atomic(cache_file, {
        "date": _prov.DATE, "report": report,
        "map": {k: list(v) for k, v in out.items()}})
    return out


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


def _get_lhb_ht():
    """hithink 龙虎榜原始 stock_items（带缓存）。失败抛异常。"""
    return _date_cached(_HT_LHB_CACHE, lambda: (
        ht.ht("special", "dragon-tiger", "--date",
              f"{_prov.DATE[:4]}-{_prov.DATE[4:6]}-{_prov.DATE[6:]}",
              timeout=90).get("stock_items") or []))


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
        prev_dates = [d for d in snapio.list_dates() if d < _prov.DATE]
        if prev_dates:
            pm = ((snapio.load(prev_dates[0]) or {}).get("modules") or {}) \
                .get("limit_up_pool") or {}
            prev_bundles = {
                "zt": pm.get("data") or [] if pm.get("status") == "ok" else []}
        return speculate.compute(_prov.DATE, bundles, prev_bundles,
                                 historical=_prov.HISTORICAL
                                 or _prov.DATE != time.strftime("%Y%m%d"))
    except Exception as e:
        return _err(e)
