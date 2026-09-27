# -*- coding: utf-8 -*-
"""大盘与外围数据模块（2026-09-27 自 providers.py 拆出，行为零变化）。

收拢大盘指数 market_indices、市场宽度 breadth、监管公告 regulatory、外围市场
global_market（含东财 push2delay 直连 _em_global_quotes）：都是 akshare 新浪/
东财通道或指数口径的「市场面」模块——大盘指数与两市成交额/大小盘同源新浪指数
spot（_common.index_spot_sina 当日一次共享），breadth 复用 _common 的全市场
快照与三池取数，监管公告与港股/美股走 akshare，日韩走东财直连。
"""
import http_retry

import providers as _prov
from ._common import (
    ak, _retry, _err, _native, _records, index_spot_sina,
    _get_market_snapshot, _get_zt_pool_ht, _get_dt_pool_ht, _get_zb_pool_ht,
)


def market_indices():
    """大盘指数：上证/深成/创业板/科创50/沪深300 最新价、涨跌幅、成交额。"""
    try:
        df = _retry(index_spot_sina)
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
        # 跌停池（hithink special）上游偶发对 ST 票的关联查询整体 500（FUYAO_5003，
        # 2026-09-22 实例：*ST亚士 603378 跌停触发 unknown market_id=22，列表级失败，
        # 任何分页/size 都避开不了）。跌停家数改用全市场快照近似：|pct|≥9.9 即视为
        # 跌停（含 ST 5cm 档会少量多计，误差远小于整个 breadth 模块失败）；
        # 模块不再因单一池子挂掉而 error——limit_down_pool 模块自身仍如实报错。
        try:
            dt_rows = _get_dt_pool_ht()
        except Exception as _dt_err:  # noqa: BLE001
            dt_rows = []
            _dt_degraded = True
            LOG_BREADTH_DEGRADED = str(_dt_err)[:120]
        else:
            _dt_degraded = False
            LOG_BREADTH_DEGRADED = None
        zb = _get_zb_pool_ht()
        max_lb = max((r.get("continue_day_cnt") or 1) for r in zt) if zt else 0
        real_zt = sum(1 for r in zt
                      if not r.get("is_st") and not r.get("is_new"))
        if _dt_degraded:
            dt_rows = [{"ticker": r.get("ticker")} for r in rows
                       if (r.get("price_change_ratio_pct") or 0) <= -9.9]

        data = {
            "上涨": up_n, "下跌": down_n, "平盘": flat_n,
            "涨停家数": len(zt), "跌停家数": len(dt_rows),
            "最高连板": max_lb, "炸板家数": len(zb),
            "真实涨停": real_zt,
        }
        if _dt_degraded:
            data["跌停家数口径"] = f"快照近似（|pct|≥9.9，上游跌停池故障: {LOG_BREADTH_DEGRADED}）"

        # 两市成交额（沪市 sh000001 + 深市 sz399106）+ 大小盘对比（沪深300 / 中证1000）
        try:
            spot = _retry(index_spot_sina)   # 与 market_indices 共享当日一次抓取
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
        except Exception as e:  # noqa: BLE001 - 指数补充失败不影响情绪主数据
            # 但也不能全静默（2026-09-14 审查：缺两市成交额无人知晓）；stdout 走
            # fetch_daily 日志（logutil 与 .bat 重定向都落 stdout）
            print(f"[warn] breadth 指数补充失败（两市成交额/大小盘缺）: "
                  f"{type(e).__name__}: {str(e)[:120]}")

        return {"status": "ok", "data": data}
    except Exception as e:
        return _err(e)


def regulatory():
    """监管榜单：当日监管相关公告，按四个分类组织。

    - abnormal_wave  股票交易异常波动公告
    - penalty        纪律处分 / 通报批评 / 监管函 / 警示函 / 监管措施 / 立案调查 / 行政处罚
    """
    try:
        df = _retry(lambda: ak.stock_notice_report(symbol="全部", date=_prov.DATE))
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


def _em_global_quotes(secids):
    """东财全球指数实时（push2delay 直连）：返回 [{名称, 最新价, 涨跌幅}]。"""
    try:
        # 出站统一收口 http_retry.guarded_get（2026-09-27，方案 O-1）；白名单字面量就地声明
        r = http_retry.guarded_get(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            allow_hosts=("push2delay.eastmoney.com",),
            params={"fltt": 2, "secids": ",".join(secids),
                    "fields": "f2,f3,f4,f12,f14"},
            timeout=12,
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
