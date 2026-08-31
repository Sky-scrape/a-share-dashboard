# -*- coding: utf-8 -*-
"""【LEGACY · 东财口径（2026-09-01 退役）】收盘后批量拉取板块当日分钟级分时。

整站板块口径已统一到同花顺一级行业 90 个，采集器见 ths_collect.py。
本文件与其 trends2/clist 接口只服务东财 BK 历史归档（daily_legacy_eastmoney/），
不再接入任何计划任务；保留仅为口径回溯参考。

原说明：
    python fetch_day.py                 # 拉取今天（默认）
    python fetch_day.py --date 2026-08-10
    python fetch_day.py --industry 70 --concepts 30   # 调整板块数量
    python fetch_day.py --refresh       # 强制刷新板块清单

说明:
    - 板块分时接口只提供当日数据，收盘后拉取可获得全天 241 个 1 分钟点
    - 盘中运行也能拉（数据只到当前时刻），配合 collect.py 做盘中监控
    - 接口有频率限制：内置限速 + 指数退避重试 + 错误隔离（单板块失败不影响其他）
"""
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import random
import sys
import time

import requests

import config

sys.path.insert(0, os.path.dirname(config.PROJECT_ROOT + os.sep))  # 项目根
sys.path.insert(0, os.path.join(config.PROJECT_ROOT, "backend"))
import lockutil  # noqa: E402

LOCK_PATH = os.path.join(config.PROJECT_ROOT, ".status", "fetch-rotation.lock")

# 禁用系统代理：直连东财接口（坏代理会导致请求挂起重试）
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
HOSTS = ["push2his.eastmoney.com", "push2delay.eastmoney.com"]
MAX_WORKERS = 3          # 并发数（东财限制严，不宜过大）
BASE_SLEEP = 0.6         # 每请求基础间隔（秒）
MAX_RETRY = 4            # 单板块最大重试次数


def _is_weekend(date_str):
    """周末判断（本地，无网络依赖）。"""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime.date(y, m, d).weekday() >= 5


def _verify_trade_day(date_str):
    """盘后场景验证：东财上证指数日 K 线最新日期 == 请求日期。

    返回 True/False；接口异常返回 None（无法验证，不阻塞抓取）。
    注意：只用于收盘后（≥15:30）场景——盘中日 K 尚未生成，验证会误判；
    指定历史日期时不适用（该接口只提供当日数据）。
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {"secid": "1.000001", "klt": "101", "fqt": "0", "lmt": "3",
              "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        d = r.json().get("data")
        if not d or not d.get("klines"):
            return None
        last = d["klines"][-1].split(",")[0]  # "2026-08-18"
        return last == date_str
    except Exception:
        return None


def fetch_trend(code):
    """拉取单个板块当日分时。返回 dict 或 None。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    params = {
        "secid": f"90.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0", "ndays": "1",
    }
    last_err = None
    for attempt in range(MAX_RETRY):
        host = random.choice(HOSTS)
        try:
            r = requests.get(f"https://{host}/api/qt/stock/trends2/get",
                             params=params, headers=HEADERS, timeout=12)
            d = r.json().get("data")
            if d and d.get("trends"):
                return d
            if d is None:
                last_err = "empty data"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(BASE_SLEEP * (2 ** attempt) + random.uniform(0, 0.5))
    return {"code": code, "error": last_err}


def parse_trend(data):
    """trends2 原始响应 -> 精简序列。"""
    pre_close = data.get("prePrice") or 0
    times, pcts, prices, amounts = [], [], [], []
    for row in data["trends"]:
        parts = row.split(",")
        t = parts[0][11:16]                 # "09:30"
        price = float(parts[2])             # f53 现价
        pct = (price / pre_close - 1) * 100 if pre_close else 0.0
        amount = float(parts[6]) if len(parts) > 6 else 0.0  # 成交额(元)
        times.append(t)
        prices.append(round(price, 2))
        pcts.append(round(pct, 2))
        amounts.append(amount)
    return {"times": times, "pcts": pcts, "prices": prices, "amounts": amounts,
            "pre_ok": pre_close > 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--industry", type=int, default=31, help="行业板块数量上限（口径=申万一级，默认全量 31）")
    ap.add_argument("--concepts", type=int, default=0, help="概念板块数量（默认不加）")
    ap.add_argument("--refresh", action="store_true", help="强制刷新板块清单")
    ap.add_argument("--force", action="store_true",
                    help="非交易日（周末/节假日）也强制抓取")
    ap.add_argument("--keep-days", type=int, default=120,
                    help="保留最近 N 天分时数据，更旧的自动清理（默认 120）")
    args = ap.parse_args()

    # 跨进程锁：手动重抓与计划任务不并发（拿到锁后退出时释放）
    lock = lockutil.acquire(LOCK_PATH)
    if lock is None:
        print(f"{args.date}: 已有轮动抓取在运行（锁 {LOCK_PATH}），本次跳过。")
        sys.exit(0)
    try:
        _run(args)
    finally:
        lockutil.release(lock)


def _run(args):
    today_s = time.strftime("%Y-%m-%d")
    if args.date != today_s:
        print(f"!! 注意：接口仅提供当日数据，--date {args.date} 将把今日行情写入该文件名（非回补）")

    # 非交易日探测（--force 跳过）
    if not args.force:
        if _is_weekend(args.date):
            print(f"{args.date}: 周末非交易日，跳过。用 --force 可强制抓取。")
            sys.exit(0)
        now = datetime.datetime.now()
        # 盘后（≥15:30）用东财日 K 线验证节假日；盘中不验证（日 K 未生成）
        if now.hour * 60 + now.minute >= 15 * 60 + 30 and args.date == today_s:
            ok = _verify_trade_day(args.date)
            if ok is False:
                print(f"{args.date}: 疑似非交易日（行情最新日期非当日），跳过。用 --force 可强制抓取。")
                sys.exit(0)
            if ok is None:
                print("!! 交易日验证接口不可达，仅按周末判断继续", file=sys.stderr)

    boards = config.fetch_boards(industry_count=args.industry,
                                 concept_count=args.concepts,
                                 refresh=args.refresh)
    pool = []
    for b in boards.get("industry", []):
        pool.append(("industry", b["f12"]))
    for b in boards.get("concept", []):
        pool.append(("concept", b["f12"]))
    print(f"共 {len(pool)} 个板块，开始拉取 {args.date} 分时数据（并发 {MAX_WORKERS}）...")

    results, failed = {}, []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_trend, code): (btype, code) for btype, code in pool}
        for fut, (btype, code) in futures.items():
            raw = fut.result()
            if "error" in raw:
                failed.append((code, raw["error"]))
                results[(btype, code)] = raw   # 失败也占位，避免类型混淆
                continue
            parsed = parse_trend(raw)
            # 名称以分时接口返回为准；市值等元数据从清单补
            meta = {}
            for b in boards[btype]:
                if b["f12"] == code:
                    meta = b
                    break
            results[(btype, code)] = {
                "code": code, "name": raw.get("name") or meta.get("f14") or code,
                "type": btype, "mcap": meta.get("f20") or 0,
                "up": meta.get("f104"), "down": meta.get("f105"),
                **parsed,
            }

    # 二轮补试：首轮失败的板块多为瞬时断连/限频，错峰休整后串行重拉一轮
    if failed:
        print(f"首轮失败 {len(failed)} 个，20s 后二轮补试: {[c for c, _ in failed]}")
        time.sleep(20)
        pool_map = {c: t for t, c in pool}
        still = []
        for code, _err in failed:
            raw = fetch_trend(code)
            if "error" not in raw:
                btype = pool_map.get(code)
                meta = next((b for b in boards.get(btype or "", []) if b["f12"] == code), {})
                parsed = parse_trend(raw)
                results[(btype, code)] = {
                    "code": code, "name": raw.get("name") or meta.get("f14") or code,
                    "type": btype, "mcap": meta.get("f20") or 0,
                    "up": meta.get("f104"), "down": meta.get("f105"),
                    **parsed,
                }
                print(f"  二轮补回：{code} {raw.get('name', '')}")
            else:
                still.append((code, raw["error"]))
        failed = still

    # 组装输出
    out = {"date": args.date, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "boards": [], "series": {}}
    for (btype, code), item in results.items():
        if "error" in item:
            continue
        out["boards"].append({"code": code, "name": item["name"], "type": item["type"],
                              "mcap": item["mcap"]})
        out["series"][code] = {k: item[k] for k in
                               ("name", "type", "times", "pcts", "prices", "amounts")}

    # 失败板块显式输出（前端可提示，避免静默缺失）
    out["failed"] = [{"code": c, "error": e} for c, e in failed]

    # 数据质量标记：无昨收(涨跌幅不可信)的板块超过阈值时置 suspect
    bad_pre = sum(1 for item in results.values()
                  if isinstance(item, dict) and item.get("pre_ok") is False)
    if bad_pre > max(3, len(out["boards"]) // 5):
        out["data_quality"] = f"suspect: {bad_pre} 个板块无昨收，涨跌幅不可信"

    # 取所有板块公共时间点（防止个别板块数据不齐）
    ok_codes = [b["code"] for b in out["boards"]]
    trimmed_note = None
    if ok_codes:
        full_len = max(len(out["series"][c]["times"]) for c in ok_codes)
        common = set(out["series"][ok_codes[0]]["times"])
        for c in ok_codes[1:]:
            common &= set(out["series"][c]["times"])
        common = sorted(common)
        if common != out["series"][ok_codes[0]]["times"]:
            trimmed_note = f"交集裁剪 {full_len - len(common)} 分钟（个别板块分钟缺口）"
            for c in ok_codes:
                s = out["series"][c]
                keep = {t: i for i, t in enumerate(s["times"]) if t in common}
                s["times"] = common
                s["pcts"] = [s["pcts"][keep[t]] for t in common]
                s["prices"] = [s["prices"][keep[t]] for t in common]
                s["amounts"] = [s["amounts"][keep[t]] for t in common]
        out["times"] = common

    # 数据覆盖度：完整交易日应为 241 个分钟点且尾点 15:00；不足即盘中采集或裁剪
    _t = out.get("times") or []
    out["coverage"] = {
        "minutes": len(_t),
        "last_time": _t[-1] if _t else None,
        "complete": len(_t) >= 241 or (_t and _t[-1] == "15:00"),
        "trimmed_note": trimmed_note,
    }

    os.makedirs(os.path.join(config.DATA_DIR, "daily"), exist_ok=True)
    path = os.path.join(config.DATA_DIR, "daily", f"{args.date}.json")
    # 原子写：先写 .tmp 再 os.replace，避免服务端读到半截 JSON
    tmp = path + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)
    print(f"完成：{len(out['boards'])} 个板块，时间点 {len(out['times'])} 个 -> {path}")
    # 任务状态：供 /api/health 数据管家展示（失败不影响主流程）
    # 注意：必须写项目根 .status/（旧版误写 backend/.status 导致 health 永远看旧数据）
    try:
        st_dir = os.path.join(config.PROJECT_ROOT, ".status")
        os.makedirs(st_dir, exist_ok=True)
        json.dump({
            "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": args.date,
            "boards": len(out["boards"]),
            "times": len(out["times"]),
            "failed": failed,
            "exit": 0,
        }, open(os.path.join(st_dir, "rotation.json"), "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        print(f"  状态写入失败: {e}")

    print(f"耗时 {time.time()-t0:.0f}s；失败 {len(failed)} 个: {failed[:8]}")

    # 数据保留：清理超过 keep_days 天的历史分时文件
    daily_dir = os.path.join(config.DATA_DIR, "daily")
    cutoff = datetime.date.today() - datetime.timedelta(days=args.keep_days)
    for fn in os.listdir(daily_dir):
        if not (len(fn) == 15 and fn.endswith(".json")):
            continue
        try:
            d = datetime.date.fromisoformat(fn[:10])
        except ValueError:
            continue
        if d < cutoff:
            os.remove(os.path.join(daily_dir, fn))
            print(f"  已清理旧数据 {fn}（超 {args.keep_days} 天）")


if __name__ == "__main__":
    main()
