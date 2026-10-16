# -*- coding: utf-8 -*-
"""主脚本：抓取当日全套复盘数据 → data/<YYYYMMDD>.json。

用法：
    python backend/fetch_daily.py                # 默认今天
    python backend/fetch_daily.py --date 20260805
    python backend/fetch_daily.py --date 20260805 --out data/tmp.json

顶层结构：{date, fetched_at, modules: {模块名: {status, data, ...}}}
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
# 行缓冲＋每次写入即落盘（2026-10-16 补 10-15 缺口事故）：.bat 把 stdout 重定向到
# 日志文件时 Python 默认块缓冲（4~8KB），被强杀（PT1H/关机）时最后一段日志连同
# 「卡在哪一步」的线索一起丢在缓冲区里——10-15 当天日志停在进度条、之后全无，
# 正是这个原因（进度条走 stderr 行缓冲才留下来）。write_through 让每行即刻可见。
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import providers
import ht
import snapio
import fsutil
import lockutil
import logutil
import app_config  # noqa: E402  运营常量薄层（backend/app_config.py，方案 S-5）

LOG = logutil.get_logger("ak.recap")

# 项目路径：backend/recap/fetch_daily.py → 项目根/data/recap
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "recap")
LOCK_PATH = os.path.join(PROJECT_ROOT, ".status", "fetch-recap.lock")

# 整轮抓取的硬上限（2026-10-16 补 10-15 缺口事故）：抓取链被系统级中断（S0 待机
# 断网 / 网络中断）时，上游无 timeout 的请求（akshare 走 requests 不设超时）会把
# 进程永久挂在某个 socket 上——10-15 当晚即为：无 traceback、无 fetch end、快照未
# 落盘，直到计划任务 PT1H 兜底杀整棵进程树，事后无从判断卡在哪一步。正常一轮
# 1~2 分钟，20 分钟足以区分「慢」与「挂死」。
FETCH_DEADLINE_S = 1200


def _arm_deadline(limit_s, log):
    """起守护线程：抓取超过 limit_s 秒仍在跑 → 记一条可诊断日志后强制终止进程。

    为什么是 os._exit：挂死的线程阻塞在内核 socket 上，无法被合作式取消，唯一
    可靠的中断就是终止进程。两项收尾显式做掉——释放抓取锁（pid 是本进程，正常
    路径；即便漏掉也有 lockutil 的 40 分钟过期接管兜底），flush 输出（否则那条
    诊断日志随进程一起消失）；快照本身走 fsutil 原子写，绝不会留半截文件。
    """
    import threading

    def _kill():
        time.sleep(limit_s)
        log.error(f"== 抓取超过硬上限 {limit_s}s 未完成，强制退出（上游请求无超时挂死？） ==")
        try:
            lockutil.release(LOCK_PATH)
        except Exception:  # noqa: BLE001 - 收尾失败不改变「必须退出」的结论
            pass
        for f in (sys.stdout, sys.stderr):
            try:
                f.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(1)

    threading.Thread(target=_kill, name="fetch-deadline", daemon=True).start()


def _is_trade_day(date):
    """交易日探测：hithink market.calendar 优先（权威），新浪日历兜底。

    返回 True/False；两次都失败返回 None（无法验证，不阻塞抓取）。
    """
    try:
        cal = ht.ht("market", "calendar")
        dates = {str(x.get("date")) for x in (cal.get("item") or [])}
        if dates:
            if date in dates:
                return True
            if date >= min(dates):  # 在日历范围内却不在其中 -> 非交易日
                return False
            # 早于日历范围的历史日期，交给新浪兑底
    except Exception as e:
        LOG.warning(f"hithink 交易日历不可用（{type(e).__name__}），改用新浪日历")
    try:
        cal = providers._retry(lambda: providers.ak.tool_trade_date_hist_sina())
        trade_dates = set(cal["trade_date"].astype(str))
        dt = datetime.datetime.strptime(date, "%Y%m%d")
        return dt.strftime("%Y-%m-%d") in trade_dates
    except Exception as e:
        LOG.warning(f"交易日历探测失败（{type(e).__name__}）")
        return None


def main():
    ap = argparse.ArgumentParser(description="A股盘后数据抓取")
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"),
                    help="8 位数字日期，如 20260805（默认今天）")
    ap.add_argument("--out", default=None, help="输出 JSON 路径（默认 data/<日期>.json）")
    ap.add_argument("--force", action="store_true",
                    help="非交易日（涨停池为空）也强制抓取")
    ap.add_argument("--keep-days", type=int, default=app_config.get("recap_keep_days"),
                    help="保留最近 N 天快照，更旧的自动清理（默认 400，可被 "
                         ".status/config.json 的 recap_keep_days 覆盖）")
    args = ap.parse_args()

    date = args.date
    if not (len(date) == 8 and date.isdigit()):
        LOG.warning(f"日期参数非法: {date!r}，应为 8 位数字如 20260805")
        sys.exit(1)

    # 跨进程锁：手动重抓与计划任务不并发
    lock = lockutil.acquire(LOCK_PATH)
    if lock is None:
        LOG.info(f"{date}: 已有复盘抓取在运行（锁 {LOCK_PATH}），本次跳过。")
        sys.exit(0)
    _arm_deadline(FETCH_DEADLINE_S, LOG)   # 挂死兜底：到点强退，好过静默挂到 PT1H
    try:
        _run(args, date)
    finally:
        lockutil.release(lock)


def _run(args, date):
    providers.set_context(date)   # 抓取上下文唯一改写入口（providers.set_context）

    # 交易日探测：hithink 日历优先，新浪兑底（静态日历，覆盖周末/法定节假日）
    if not args.force:
        trade = _is_trade_day(date)
        if trade is False:
            LOG.info(f"{date}: 非交易日（周末/法定节假日），已跳过。用 --force 可强制抓取。")
            sys.exit(0)

    # 盘前/盘中拒绝抓当日（20260930 事故修复）：涨停/跌停/炸板池、龙虎榜等盘后数据
    # 收盘（15:00）后才存在，提前抓只会写出全 0 空快照占住当日槽位——09-30 的空快照
    # 正是 08:58 手动运行写入、此后 17:05 任务连崩 7 天没被覆盖。--force 可显式越过。
    if (not args.force and date == time.strftime("%Y%m%d")
            and datetime.datetime.now().time() < datetime.time(15, 0)):
        LOG.warning(f"{date}: 未到收盘（15:00），盘后数据（涨跌停池/龙虎榜）尚不存在，"
                    f"拒绝抓取以免空快照占位；确要提前抓请加 --force。")
        sys.exit(0)

    LOG.info(f"== 抓取 {date} 全套数据 ==")
    results = providers.fetch_all()

    # 失败模块重试（2026-09-11，09-10 上游抖动致 4 模块静默失败的补救）：
    # 只重抓失败模块，间隔递增；重试后仍失败则快照照常落盘但进程以非零码退出
    retry_rounds = (30, 60)   # 重试前等待秒数（上游抖动通常分钟级恢复）
    failed = [n for n in providers.MODULES if results[n].get("status") != "ok"]
    for attempt, delay in enumerate(retry_rounds, 1):
        if not failed:
            break
        LOG.warning(f"  {len(failed)} 个模块失败，{delay}s 后重试"
                    f"（第 {attempt}/{len(retry_rounds)} 轮）: " + "、".join(failed))
        time.sleep(delay)
        for name in list(failed):
            try:
                results[name] = getattr(providers, name)()
            except Exception as e:  # noqa: BLE001
                results[name] = {"status": "error",
                                 "error": f"{type(e).__name__}: {str(e)[:160]}"}
        failed = [n for n in providers.MODULES if results[n].get("status") != "ok"]

    for name in providers.MODULES:
        st = results[name]["status"]
        extra = ""
        if st == "ok":
            data = results[name].get("data")
            extra = f" ({len(data)} 条)" if isinstance(data, (list, dict)) else ""
        else:
            extra = f" -> {results[name].get('error', '')[:80]}"
        LOG.info(f"  {name}: {st}{extra}")

    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
    if failed:
        LOG.error(f"== 采集不完整：{ok_count}/{len(results)} ok，"
                  f"重试 {len(retry_rounds)} 轮后仍失败: " + "、".join(failed) + " ==")
    else:
        LOG.info(f"== 完成：{ok_count}/{len(results)} 模块 ok ==")

    # 昨日两市成交额对比（读 data/recap/ 下最近的历史快照，兼容 gzip）
    if results.get("breadth", {}).get("status") == "ok":
        try:
            prev_dates = [d for d in snapio.list_dates(DATA_DIR) if d < date]
            if prev_dates:
                prev = snapio.load(prev_dates[0], DATA_DIR)
                pv = ((prev or {}).get("modules", {}).get("breadth", {})
                      .get("data", {}).get("两市成交额"))
                if pv is not None:
                    results["breadth"]["data"]["昨日两市成交额"] = pv
                    LOG.info(f"  昨日两市成交额对比: {pv:,.0f}")
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"  昨日成交额对比失败（忽略）: {type(e).__name__}")

    # 昨日涨停名单注入（前端速览展示用）：派生层晋级率已改为按日期相邻现算，不再依赖本字段
    prev_zt = {}
    try:
        prev_dates = [d for d in snapio.list_dates(DATA_DIR) if d < date]
        if prev_dates:
            pp = snapio.load(prev_dates[0], DATA_DIR) or {}
            rows = pp.get("modules", {}).get("limit_up_pool", {}).get("data", []) or []
            prev_zt = {
                # list_dates 倒序（最新在前）：取数用的是 prev_dates[0]（最近一
                # 交易日），日期标签必须同源——旧代码误取 prev_dates[-1]（约 9
                # 个月前的最老快照日期），标签与数据来源错位（2026-09-30 修复）
                "date": prev_dates[0],
                "zt_codes": [str(r["代码"]) for r in rows if r.get("代码") is not None],
                "zt_count": len(rows),
                "max_lb": max((int(r.get("连板数") or 0) for r in rows), default=0),
            }
            # 昨日涨停股今日表现（复用全A快照缓存，不额外抓接口）
            today_res = providers.prev_zt_today(prev_zt["zt_codes"])
            prev_zt["today"] = today_res
            if today_res["status"] == "ok":
                LOG.info(f"  昨日涨停今日表现: {len(today_res['data'])}/{prev_zt['zt_count']} 只匹配")
    except Exception as e:  # noqa: BLE001
        LOG.warning(f"  昨日名单注入失败（忽略）: {type(e).__name__}")

    out = args.out or None
    snapshot = {
        "date": date,
        "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "modules": results,
        "prev": prev_zt,
    }
    if args.out:
        if ".." in args.out or not re.fullmatch(r"[\w\/:.\-]+\.json", args.out):
            raise SystemExit(f"非法输出路径: {args.out}")
        fsutil.save_json_atomic(args.out, snapshot, indent=2)   # 原子写
        LOG.info(f"已写入 {args.out}")
    else:
        out = snapio.save(date, snapshot, DATA_DIR)
        LOG.info(f"已写入 {out}")

    # 任务状态：供 /api/health 数据管家展示（失败不影响主流程）+ 触发派生层重算
    try:
        st_dir = os.path.join(PROJECT_ROOT, ".status")
        fsutil.save_json_atomic(os.path.join(st_dir, "recap.json"), {
            "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": date,
            "modules": {k: v.get("status", "?") for k, v in results.items()},
            "failed": failed,
            "exit": 1 if failed else 0,
        })
    except Exception as e:
        LOG.info(f"  状态写入失败: {e}")
    if not args.out:
        try:
            import derive
            derive.derive_recap(force=True)
        except Exception as e:
            LOG.warning(f"  派生层重算失败（忽略，server 会懒重算）: {type(e).__name__}")


    # 数据保留：只保留最近 N 天快照（默认 400 天，见 --keep-days），避免磁盘无限膨胀
    keep_days = args.keep_days
    data_dir = DATA_DIR
    cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_days)
    for fn in os.listdir(data_dir):
        if fn.endswith(".json"):
            stem = fn[:-5]
        elif fn.endswith(".json.gz"):
            stem = fn[:-8]
        else:
            continue
        if not (len(stem) == 8 and stem.isdigit()):
            continue
        try:
            d = datetime.datetime.strptime(stem, "%Y%m%d")
        except ValueError:
            continue
        if d < cutoff:
            os.remove(os.path.join(data_dir, fn))
            LOG.info(f"  已清理旧数据 {fn}（超 {keep_days} 天）")

    # 非零退出码：快照已落盘（部分数据好过没有），但让计划任务/日志可见本次不完整
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
