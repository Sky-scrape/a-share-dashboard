# -*- coding: utf-8 -*-
"""板块类数据模块（2026-09-27 自 providers.py 拆出，行为零变化）。

收拢行业板块 boards 与概念板块 concepts：两者同用 hithink 的目录（881 行业 /
cn 概念）+ index.snapshot 批量秒级快照 + 当日快照进程内缓存（_HT_CONCEPTS_
CACHE），boards 另走 index_hist_cache 增量日线（与 speculate.industry_series
共用同一份 .ht_cache 文件），内聚一处。
"""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import ht
import index_hist_cache

import providers as _prov
from ._common import (
    _err, _round2, _date_cached, _HT_CONCEPTS_CACHE,
    _get_industries, _get_concepts,
)


def boards():
    """行业板块：同花顺一级行业 90 个（hithink index）。

    近 5 日 = index.history（2026-09-10 起走 index_hist_cache 增量缓存，与
    speculate.industry_series 共用同一份 .ht_cache 文件：首次全量回溯 1500 天、
    之后每日只补尾段，不再每天 90 次全量 10 日子进程）；
    当日涨跌幅优先取自历史日线当天（支持历史日期回补），index.snapshot 仅兜底。
    输出结构与旧版一致：{名称, 涨跌幅, history: [{日期, 收盘价, 涨跌幅}]}。
    """
    try:
        industries = _get_industries()
        codes = [c for c, _ in industries]
        snap = ht.index_snapshot_batches(codes)
        today_s = datetime.strptime(_prov.DATE, "%Y%m%d").strftime("%Y-%m-%d")

        def fetch_hist(code):
            try:
                # code 来自 _get_industries()，已是 "881xxx.TI" 形态——
                # 2026-09-20 修复：原先再拼一次 ".TI" 得到 "881xxx.TI.TI"，被上游
                # 参数校验拒绝（CLI_BAD_ARGUMENT），异常被下面 except 吞掉后降级成
                # 空历史，导致 09-10 起快照 boards.history 只剩当天 1 条、前端
                # 「点行业看近5日走势」画不出折线。index_hist_cache._file_key 对
                # ".TI" 后缀已有归一，直接传 code 即命中同一份缓存。
                return code, index_hist_cache.series(code, _prov.DATE)
            except Exception:  # noqa: BLE001 - 单行业历史失败跳过
                return code, []

        with ThreadPoolExecutor(max_workers=8) as ex:
            hists = dict(ex.map(fetch_hist, codes))

        result = []
        for code, name in industries:
            row = snap.get(code)
            if row is None:
                continue
            # 只看 ≤DATE 的历史（缓存可能已被别的调用补到更新日期）
            hist_raw = [r for r in (hists.get(code) or []) if r[0] <= today_s]
            hist_raw = hist_raw[-10:]   # 与旧实现同窗（近 10 日，输出取尾 5）
            hist = []
            for i, hrow in enumerate(hist_raw):
                close = hrow[1]
                prev_close = hist_raw[i - 1][1] if i > 0 else None
                hist.append({
                    "日期": hrow[0], "收盘价": _round2(close),
                    "涨跌幅": None if not prev_close else _round2(
                        (close - prev_close) / prev_close * 100.0),
                })
            # 涨跌幅优先取自历史日线当天（支持历史日期回补），快照仅兜底
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


def concepts():
    """概念板块：同花顺概念指数当日快照（hithink，批量秒级）。

    输出：概念 / 涨跌幅 / 成交额(亿) / 涨幅排名。
    按 DATE 进程内缓存（2026-09-10）：fetch_all 的 concepts 模块与 speculation
    bundles 同日各取一次，不再重复抓 390 概念。
    （旧版 info_ths 的资金净流入/涨跌家数同花顺批量接口不再提供，已移除）
    """
    try:
        def _fetch():
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
                raise RuntimeError("ValueError: 概念数据为空")
            rows.sort(key=lambda x: x["涨跌幅"] if x["涨跌幅"] is not None else -999,
                      reverse=True)
            for i, r in enumerate(rows, 1):
                r["涨幅排名"] = i
            return rows
        rows = _date_cached(_HT_CONCEPTS_CACHE, _fetch)
        return {"status": "ok", "data": rows}
    except Exception as e:
        return _err(e)
