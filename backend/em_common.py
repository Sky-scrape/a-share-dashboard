# -*- coding: utf-8 -*-
"""东财 push2 HTTP 直连 · 共享封装（2026-09-04 收敛）。

EM_HEADERS/EM_HOSTS/em_kline 曾在 recap/backfill_full.py、rotation/backfill_rotation.py、
rotation/fetch_day.py、rotation/config.py 四处复制（上游改字段/换主机要同步四处）。
活跃路径（两个回补脚本）全部收到这里；LEGACY 退役脚本（fetch_day/config）按 README
约定保留原样不动，仅作口径回溯参考。
"""
import os

import requests

import http_retry  # noqa: E402  共享重试（backend/http_retry.py：指数退避+可重试判定）

# 直连东财：坏系统代理会导致请求挂起（与 server/providers 同一约定）
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
EM_HOSTS = ["push2his.eastmoney.com", "push2delay.eastmoney.com"]


def em_kline_raw(secid, klt, lmt, tries=3, timeout=12,
                 fields1="f1,f2,f3", fields2="f51,f52,f53,f54,f55,f56,f57"):
    """东财 K 线原始 data（含 name/klines），轮换主机重试；全部失败返回 None。

    重试统一走 backend/http_retry.retry（指数退避 + 网络类判定）；主机轮换放进
    尝试函数内部（每次尝试换下一台），空 klines 视为可重试（上游瞬时抖动），
    与旧实现「换主机 + 递增 sleep」语义一致。
    """
    params = {"secid": secid, "klt": str(klt), "fqt": "0",
              "lmt": str(lmt), "end": "20500101",
              "fields1": fields1, "fields2": fields2}
    state = {"i": 0}

    def _attempt():
        host = EM_HOSTS[state["i"] % len(EM_HOSTS)]
        state["i"] += 1
        r = requests.get(f"https://{host}/api/qt/stock/kline/get",
                         params=params, headers=EM_HEADERS, timeout=timeout)
        d = r.json().get("data")
        if d and d.get("klines"):
            return d
        raise RuntimeError(f"em klines 空（{host}）")

    def _judge(e):
        return isinstance(e, RuntimeError) or http_retry.is_retryable(e)

    try:
        return http_retry.retry(_attempt, tries=tries - 1, delay=1.5,
                                retry_on=_judge)
    except Exception:  # noqa: BLE001 - 重试用尽返回 None（调用方按缺失降级）
        return None


def em_kline_rows(secid, klt, lmt, tries=3, timeout=12):
    """东财日K → 行列表（date/open/close/high/low/amount）。

    fields2 f51日期 f52开 f53收 f54高 f55低 f56量 f57额；无数据返回 []。"""
    d = em_kline_raw(secid, klt, lmt, tries=tries, timeout=timeout)
    kl = (d or {}).get("klines") or []
    rows = []
    for k in kl:
        p = k.split(",")
        rows.append({"date": p[0][:10], "open": float(p[1]),
                     "close": float(p[2]), "high": float(p[3]),
                     "low": float(p[4]),
                     "amount": float(p[6]) if len(p) > 6 else 0.0})
    return rows
