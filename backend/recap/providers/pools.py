# -*- coding: utf-8 -*-
"""池类数据模块（2026-09-27 自 providers.py 拆出，行为零变化）。

收拢涨停池 / 跌停池 / 炸板池 / prev_zt_today（昨日涨停今日表现，晋级率原料）
四个池子语义函数：三者同用 hithink special 池接口（取数与按 DATE 缓存原语在
_common），且都依赖全市场快照补成交额、行业映射补所属行业，内聚一处。

日期上下文（DATE/HISTORICAL）真身在包 __init__.py，这里经 `import providers`
在调用时读包属性，外部改写 providers.DATE/HISTORICAL 的旧语义保持不变。
"""
import providers as _prov
from ._common import (
    _err, _norm_code, _round2,
    _get_zt_pool_ht, _get_dt_pool_ht, _get_zb_pool_ht,
    _snap_by_code, _get_industry_map,
)


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


def limit_up_pool():
    """涨停池：hithink（同花顺口径），含连板数/封板时间/封板资金/涨停原因。

    成交额由全市场快照按代码补齐（历史日期快照不可用则留空）；
    所属行业由 index.constituents 映射补齐。
    """
    try:
        zt = _get_zt_pool_ht()
        snap = {} if _prov.HISTORICAL else _snap_by_code()
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
        snap = {} if _prov.HISTORICAL else _snap_by_code()
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
