# -*- coding: utf-8 -*-
"""研究库日线缺口备源 · 腾讯前复权日线（2026-09-15）。

背景（0914 实例）：hithink 上游「T 日 release ≈17:11 发布」与 17:05 sync 存在竞态，
0914 未发布 → 本地 DuckDB v_daily_qfq 整层缺 T 日 → 验证把正常交易的标的
（超声电子当日 3 连板）标成「停牌/数据缺失」。本模块在缺行时用腾讯日线兜底：

- 选腾讯不选东财：本网络环境东财 push2 系列接口直连不通（README 全球总览同款
  结论），web.ifzq.gtimg.cn 是 us_market 每日在用的生产直连源；
- 前复权（qfq）与 v_daily_qfq 同口径换算 o/h/l/c（%相对前一根 bar 收盘，LAG 语义）；
- 腾讯日线无成交额字段，量比 amtr 用「量比 × 价比」估计
  （amtr ≈ (vol_T/vol_T-1) × (close_T/close_T-1)，成交额=均价×量的近似，
  与 DuckDB 额比口径存在均价偏离误差），行带 src="em" 标记来源；
- 只面向验证这类小集合（≤20 只/日），每代码一次 K 线请求，单只失败不连坐；
- 真停牌/退市两源都无 T 日 bar，返回时如实缺席——备源不掩盖事实；
- 腾讯 secid 前缀：沪=sh、深=sz（北交所不在池口径内，不猜前缀）。

不碰池构建侧的 scan_trend/scan_deviation（全市场扫描无法逐只兜底）——那一侧
由 us_close_task 链前移的 data sync + 研究库 stale 告警（.status/duckdb.json）负责。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import http_retry  # noqa: E402  共享重试（backend/http_retry.py）
import netguard  # noqa: E402  SSRF 防线单一来源（backend/netguard.py）
from spec_rules import _date_iso, thscode_of  # noqa: E402  底层口径/工具单一来源

# 字面量 URL + 主机白名单（安全扫描口径；不从外部输入拼协议/域名）
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_ALLOWED_HOSTS = {"web.ifzq.gtimg.cn"}
_LMT = 12      # 覆盖 T 与前一交易日（容忍短暂停牌间隙）
_TRIES = 2     # 备源是兜底路径，快速失败优于久等


def _symbol_of(code6):
    """6 位代码 → 腾讯 A 股 symbol（sh605258/sz000823）；无法识别返回 None。"""
    t = thscode_of(code6)
    if not t:
        return None
    code, _, mkt = t.partition(".")
    pre = {"SH": "sh", "SZ": "sz"}.get(mkt)
    return pre + code if pre else None


def _safe_get(url, params, timeout=10):
    """SSRF 防线：协议/主机白名单 + 解析 IP 受限段阻断（netguard 单一来源）。

    原为 us_market._safe_get 的手工副本，2026-09-24 收拢——两份实现曾同时把本地
    代理 fake-IP 段（198.18.0.0/15）误判为受限地址，备源因此整体静默失效。
    """
    return netguard.safe_get(url, _ALLOWED_HOSTS, params=params, timeout=timeout)


def fetch_bars(code6, date8):
    """腾讯前复权日线（截至 date8 前后）：[{date, open, close, high, low, volume}]；失败 []。"""
    sym = _symbol_of(code6)
    if not sym:
        return []

    def _get():
        r = _safe_get(_KLINE_URL, {"param": f"{sym},day,,,{_LMT},qfq"})
        node = ((r.json().get("data") or {}).get(sym) or {})
        return node.get("qfqday") or node.get("day") or []

    try:
        raw = http_retry.retry(_get, tries=_TRIES, delay=1.5)
    except Exception:  # noqa: BLE001 - 备源失败返回空（调用方如实缺席）
        return []
    bars = []
    for row in raw or []:
        try:
            bars.append({"date": str(row[0]), "open": float(row[1]),
                         "close": float(row[2]), "high": float(row[3]),
                         "low": float(row[4]), "volume": float(row[5])})
        except (IndexError, TypeError, ValueError):
            continue
    return bars


def ohlc_from_bars(iso, bars):
    """日线 bars → T 日 {o,h,l,c,amtr,src:"em"}；缺 T 日 bar 或前收返回 None。

    与 spec_duckdb.ohlc_rets 的 DuckDB 口径同构：涨幅相对**前一根 bar 收盘**
    （LAG 语义，非自然日前收）；amtr 为估计口径（量比 × 价比，见模块 docstring）。
    高于 T 日 date 的行（腾讯盘中会带当日未收盘 bar）不参与定位。"""
    rows = sorted((b for b in (bars or []) if b.get("date")),
                  key=lambda b: str(b["date"])[:10])
    idx = next((i for i, b in enumerate(rows) if str(b["date"])[:10] == iso), None)
    if idx is None or idx == 0:
        return None
    t, p = rows[idx], rows[idx - 1]
    try:
        pc = float(p["close"])
        if pc <= 0:
            return None

        def _pct(v):
            return None if v is None else round((float(v) / pc - 1) * 100, 2)

        vol_t, vol_p = float(t.get("volume") or 0), float(p.get("volume") or 0)
        close_t = float(t["close"])
        amtr = (round((vol_t / vol_p) * (close_t / pc), 2)
                if vol_t > 0 and vol_p > 0 else None)
        return {"o": _pct(t.get("open")), "h": _pct(t.get("high")),
                "l": _pct(t.get("low")), "c": _pct(close_t),
                "amtr": amtr, "src": "em"}
    except (KeyError, TypeError, ValueError):
        return None


def day_rets(date8, codes):
    """缺 T 日线的 codes → 腾讯备源 {code6: {o,h,l,c,amtr,src:"em"}}。

    单只失败/缺 bar 不连坐；绝不抛错（调用方兜底路径不允许再炸）。"""
    iso = _date_iso(date8)
    out = {}
    for code in codes:
        row = ohlc_from_bars(iso, fetch_bars(code, date8))
        if row:
            out[str(code)] = row
    return out
