# -*- coding: utf-8 -*-
"""东财 push2 HTTP 直连 · 共享封装（2026-09-04 收敛）。

EM_HEADERS/EM_HOSTS/em_kline 曾在 recap/backfill_full.py、rotation/backfill_rotation.py、
rotation/fetch_day.py、rotation/config.py 四处复制（上游改字段/换主机要同步四处）。
活跃路径（两个回补脚本）全部收到这里；LEGACY 退役脚本（fetch_day/config）按 README
约定保留原样不动，仅作口径回溯参考。
"""
import os
import time

import requests

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
    """东财 K 线原始 data（含 name/klines），轮换主机重试；全部失败返回 None。"""
    params = {"secid": secid, "klt": str(klt), "fqt": "0",
              "lmt": str(lmt), "end": "20500101",
              "fields1": fields1, "fields2": fields2}
    for i in range(tries):
        host = EM_HOSTS[i % len(EM_HOSTS)]
        try:
            r = requests.get(f"https://{host}/api/qt/stock/kline/get",
                             params=params, headers=EM_HEADERS, timeout=timeout)
            d = r.json().get("data")
            if d and d.get("klines"):
                return d
        except Exception:  # noqa: BLE001 - 单主机失败换下一个，重试用尽返回 None
            pass
        time.sleep(1.5 * (i + 1))
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
