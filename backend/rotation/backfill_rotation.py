# -*- coding: utf-8 -*-
"""轮动分时历史回补：用东财 5 分钟 K 线合成缺失日期。

背景：东财 trends2 分时接口只有当日数据，但 kline/get 的 klt=5（5 分钟 K）
保留约两周历史。本脚本拉取板块 5 分钟 K，按天切片合成与 fetch_day.py 相同
结构的轮动 JSON（时间点粒度 5 分钟，每日 48 点）。

用法：
    python backend/rotation/backfill_rotation.py            # 自动补缺口
    python backend/rotation/backfill_rotation.py --from 2026-08-24
    python backend/rotation/backfill_rotation.py --force 2026-08-26
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

import em_common  # noqa: E402  东财直连共享封装（backend/em_common.py）
import fsutil     # noqa: E402  原子写盘单一来源（backend/fsutil.py）

os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

MAX_RETRY = 4
MAX_WORKERS = 1
BASE_SLEEP = 1.5

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DAILY_DIR = os.path.join(config.DATA_DIR, "daily")


def em_kline(secid, klt, lmt):
    """东财 K 线：返回 data（含 name/klines）或 None（实现在 backend/em_common.py）。"""
    return em_common.em_kline_raw(secid, klt, lmt, tries=MAX_RETRY,
                                  fields1="f1,f2,f3,f4,f5,f6")


def trading_dates_calendar():
    """hithink 交易日历（8 位），失败退周末判断。"""
    try:
        cal = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": "1.000001", "klt": "101", "fqt": "0",
                    "lmt": "40", "end": "20500101",
                    "fields1": "f1,f2,f3", "fields2": "f51"},
            headers=em_common.EM_HEADERS, timeout=12)
        kl = (cal.json().get("data") or {}).get("klines") or []
        dates = sorted(k.split(",")[0][:10].replace("-", "") for k in kl)
        if dates:
            return dates
    except Exception:  # noqa: BLE001
        pass
    out, cur = [], datetime.date(2026, 8, 1)
    while cur <= datetime.date.today():
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += datetime.timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser(description="轮动分时历史回补（5分钟K合成）")
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--force", dest="force_date", default=None,
                    help="强制重抓某日期 YYYY-MM-DD")
    args = ap.parse_args()

    boards = config.fetch_boards()
    pool = [("industry", b["f12"], b.get("f14") or "", b.get("f20") or 0)
            for b in boards.get("industry", [])]
    print(f"板块清单: {len(pool)} 个")

    cal = trading_dates_calendar()
    existing = set()
    if os.path.isdir(DAILY_DIR):
        existing = {f[:-5].replace("-", "") for f in os.listdir(DAILY_DIR)
                    if f.endswith(".json")}
    if args.force_date:
        targets = [args.force_date.replace("-", "")]
        existing.discard(targets[0])
    else:
        targets = [d for d in cal
                   if d not in existing and d >= "20260810"]
    if args.date_from:
        targets = [d for d in targets if d >= args.date_from.replace("-", "")]
    if not targets:
        print("无缺口，无需回补。")
        return
    print(f"回补目标: {targets}")

    # 每板块一次拉 5 分钟 K（约 12 个交易日窗口）+ 日 K 前收
    span_days = (datetime.date.today()
                 - datetime.datetime.strptime(targets[0], "%Y%m%d").date()).days
    lmt5 = min(600, (span_days + 6) * 48 + 48)

    results = {}
    failed = []

    def fetch_one(item):
        btype, code, fname, mcap = item
        m5 = em_kline(f"90.{code}", 5, lmt5)
        if not m5:
            return code, None, None
        d1 = em_kline(f"90.{code}", 101, 30)
        return code, m5, (d1 or {}).get("klines") or []

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for code, m5, d1 in ex.map(fetch_one, pool):
            if not m5:
                failed.append(code)
                continue
            results[code] = (m5, d1)
    print(f"K线拉取完成: {len(results)}/{len(pool)} 板块，耗时 {time.time()-t0:.0f}s")
    if failed:
        print("  失败板块:", failed[:10])

    name_by_code = {code: (fname or code) for btype, code, fname, _m in pool}
    mcap_by_code = {code: m for _b, code, _f, m in pool}

    # 按天合成
    for date in targets:
        ds = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        out = {"date": ds, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "boards": [], "series": {}, "failed": [],
               "backfill": "5m-kline"}
        ok_cnt = 0
        common_times = None
        for btype, code, _f, _m in pool:
            if code not in results:
                continue
            m5, d1 = results[code]
            # 日线 -> 前一交易日收盘
            prev_close = None
            day_closes = []
            for k in (d1 or []):
                p = k.split(",")
                day_closes.append((p[0][:10], float(p[2])))
            for i, (dc, cc) in enumerate(day_closes):
                if dc == ds and i > 0:
                    prev_close = day_closes[i - 1][1]
                    break
            if prev_close is None:
                continue
            # 5 分钟 K 切片当天
            times, pcts, prices, amounts = [], [], [], []
            for k in m5:
                if not k.startswith(ds):
                    continue
                p = k.split(",")
                t = p[0][11:16]
                close = float(p[2])
                amount = float(p[6]) if len(p) > 6 else 0.0
                times.append(t)
                prices.append(round(close, 2))
                pcts.append(round((close / prev_close - 1) * 100, 2))
                amounts.append(amount)
            if len(times) < 10:
                continue
            # 日线收盘兜底最后一个点
            day_close = next((c for dc, c in day_closes if dc == ds), None)
            if day_close and times and times[-1] < "15:00":
                times.append("15:00")
                prices.append(round(day_close, 2))
                pcts.append(round((day_close / prev_close - 1) * 100, 2))
                amounts.append(0.0)
            out["boards"].append({"code": code,
                                  "name": name_by_code.get(code, code),
                                  "type": btype, "mcap": mcap_by_code.get(code, 0)})
            out["series"][code] = {"name": name_by_code.get(code, code),
                                   "type": btype, "times": times,
                                   "pcts": pcts, "prices": prices,
                                   "amounts": amounts}
            ok_cnt += 1
            ct = set(times)
            common_times = ct if common_times is None else (common_times & ct)
        if ok_cnt < 30:
            print(f"  {ds}: 可用板块仅 {ok_cnt} 个（<30），不写入，留待重试")
            continue
        if common_times is None:
            common_times = set()
        common = sorted(common_times)
        for c in out["series"]:
            s = out["series"][c]
            keep = {t: i for i, t in enumerate(s["times"]) if t in common}
            s["times"] = common
            s["pcts"] = [s["pcts"][keep[t]] for t in common]
            s["prices"] = [s["prices"][keep[t]] for t in common]
            s["amounts"] = [s["amounts"][keep[t]] for t in common]
        out["times"] = common
        out["data_quality"] = f"backfill-5m: 合成数据，粒度 5 分钟（{ok_cnt} 板块）"

        os.makedirs(DAILY_DIR, exist_ok=True)
        path = os.path.join(DAILY_DIR, f"{ds}.json")
        fsutil.save_json_atomic(path, out)   # 原子写（backend/fsutil.py）
        print(f"  {ds}: {ok_cnt} 板块, {len(common)} 时间点 -> {path}")


if __name__ == "__main__":
    main()
