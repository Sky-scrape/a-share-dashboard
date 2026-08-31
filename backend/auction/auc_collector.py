# -*- coding: utf-8 -*-
"""实时竞价采集器（落盘 data/auction/，server/前端只读）。

时间线（交易日，计划任务 09:14 触发）：
    09:15:00-09:24:30  live 快照每 30s 一轮（观察池分批 ≤90，批间隔 3s）
    09:25:10           final 终态一次
    随后                auction-benchmark 基准一次 + 一级行业指数开盘缺口一次（sector，
                       盘后补抓同样成立；定盘时刻 open 未就绪时退避重试）

产出（全部原子写）：
    data/auction/live.json       最新一轮 live（含完整 items）
    data/auction/final.json      9:25 终态（provisional=true 表示上游当时未返回终态）
    data/auction/series.json     当日全部轮次（单股竞价曲线用，手动补抓也会写）
    data/auction/benchmark.json  竞价短期基准（含板块 tags）
    data/auction/sector.json     一级行业指数开盘缺口（全成分加权，盘后补抓仍成立）
    data/auction/industry_map.json 个股→一级行业全量映射（每 3 天到期重建，消灭「未分类」）
    .status/auction.json         任务状态（/api/health 用；非交易日/空池也会写状态与原因）

用法：
    python backend/auction/auc_collector.py           # 按当日时间线
    python backend/auction/auc_collector.py --once    # 立即补抓 final+基准（盘后/手动）
"""
import argparse
import datetime
import os
import sys
import time

# 注意：不在模块级 reconfigure stdout。本文件会被 tests/smoke.py 导入做单元校验，
# 模块级改写会把宿主进程的输出编码一并发，日志变成 GBK/UTF-8 混排。
# 作为脚本直跑时由下方 __main__ 或 auction_task.bat 的 PYTHONIOENCODING=utf-8 保证。
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend", "recap"))

import ht                      # noqa: E402
import lockutil                # noqa: E402
import auc_config as C         # noqa: E402
import auc_industry as AUI     # noqa: E402
import trade_cal               # noqa: E402  交易日历判定单一来源（backend/trade_cal.py）
import logutil                 # noqa: E402  统一 logging（时间戳/级别）

LOG = logutil.get_logger("ak.auction")


def _today_at(hms):
    n = datetime.datetime.now()
    return time.mktime(n.date().timetuple()) + hms[0] * 3600 + hms[1] * 60 + hms[2]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# 集合竞价逐轮窗口：09:15:00–09:25:59（含 09:25:10 定盘）。
# 窗口外的任何一次抓取都只能拿到冻结的定盘值（已实测：盘后 stage=live 与 stage=final 返回同一份数据），
# 把它们当逐轮画进曲线就是一条假趋势，所以轮次必须带这个标记。
_WIN_LO = 9 * 3600 + 15 * 60
_WIN_HI = 9 * 3600 + 25 * 60 + 59


def _in_window(t=None):
    """struct_time（默认当前）是否落在竞价窗口。"""
    t = t or time.localtime()
    secs = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    return _WIN_LO <= secs <= _WIN_HI


def round_in_window(rnd):
    """读轮次的竞价窗口归属：新数据看 in_window 字段，旧数据从 ts 时刻回推。"""
    flag = (rnd or {}).get("in_window")
    if isinstance(flag, bool):
        return flag
    ts = str((rnd or {}).get("ts") or "")
    hhmmss = ts[11:19]
    if len(hhmmss) != 8:
        return False
    hh, mm, ss = (int(x) for x in hhmmss.split(":"))
    return _WIN_LO <= hh * 3600 + mm * 60 + ss <= _WIN_HI


def is_trade_today():
    """hithink 交易日历探测；无法验证返回 None（按交易日继续，周末靠计划任务规避）。

    判定单一来源 backend/trade_cal.py（两侧只取数字比、拿不到可用日期返回 None
    继续采——早期 "%Y-%m-%d" vs 8 位不一致曾让每个交易日被判成非交易日、
    采集静默停摆一个月，详见该模块 docstring）。cal 以 lambda 注入：
    本模块的 ht 全局在调用时才解析，smoke 换桩测试照常生效。
    """
    return trade_cal.is_trade_today(cal=lambda: ht.ht("market", "calendar"))


def _fetch_chunk(chunk, stage):
    """单批抓取；参数校验错误时二分隔离坏代码（单只仍错则丢弃并记录）。
    返回 (items, meta, err)；meta 含上游 auction_phase/data_status。"""
    try:
        d = ht.ht("market", "auction-snapshot",
                  "--thscodes", ",".join(chunk), "--stage", stage)
        meta = {"phase": d.get("auction_phase"), "status": d.get("data_status")}
        return d.get("item") or [], meta, None
    except RuntimeError as e:
        msg = str(e)
        if "CLI_BAD_ARGUMENT" in msg and len(chunk) > 1:
            mid = len(chunk) // 2
            a, ma, ea = _fetch_chunk(chunk[:mid], stage)
            b, mb, eb = _fetch_chunk(chunk[mid:], stage)
            return a + b, (ma or mb), (ea or eb)
        return [], None, f"{chunk[:3]}…: {msg[:120]}"


def fetch_auction(codes, stage):
    """观察池分批抓取，返回 (items, errors, meta)。"""
    items, errors, meta = [], [], None
    for i in range(0, len(codes), C.BATCH):
        chunk = codes[i:i + C.BATCH]
        if i:
            time.sleep(C.BATCH_GAP)
        got, m, err = _fetch_chunk(chunk, stage)
        items.extend(got)
        meta = meta or m
        if err:
            errors.append(err)
    return items, errors, meta


def collect_round(codes, stage, label):
    t0 = time.time()
    items, errors, meta = fetch_auction(codes, stage)
    t_end = time.localtime()
    LOG.info(f"[{label}] {stage}: {len(items)}/{len(codes)} 只，"
          f"用时 {time.time() - t0:.1f}s" + (f"，错误批 {len(errors)}" if errors else ""))
    for err in errors[:3]:
        LOG.warning(f"  err: {err}")
    return {"label": label, "ts": time.strftime("%Y-%m-%d %H:%M:%S", t_end), "stage": stage,
            "in_window": _in_window(t_end),          # 窗口外只有定格值，不得当逐轮画
            "phase": (meta or {}).get("phase"),
            "data_status": (meta or {}).get("status"),
            "count": len(items), "codes_total": len(codes),
            "errors": errors[:6], "items": items}


def fetch_benchmark():
    """竞价短期基准（失败不阻塞主流程）。"""
    try:
        d = ht.ht("market", "auction-benchmark")
        C.save_json("benchmark.json", {"date": d.get("date"),
                                       "fetched_at": _now(),
                                       "items": d.get("item") or []})
        LOG.info(f"benchmark: {len(d.get('item') or [])} 条")
    except RuntimeError as e:
        LOG.warning(f"benchmark 失败（忽略）: {str(e)[:150]}")


def _sector_rows():
    """抓一轮 90 个一级行业指数的开盘缺口；返回行列表，异常返回 None。"""
    try:
        cats = ht.catalog("industry",
                          cache_dir=os.path.join(PROJECT_ROOT, "data", "cache"))
        codes = [c["thscode"] for c in cats if str(c.get("thscode", "")).startswith("881")]
        names = {c["thscode"]: c.get("name", "") for c in cats}
        snap = ht.index_snapshot_batches(codes)
        rows = []
        for code in codes:
            r = snap.get(code) or {}
            o, p = r.get("open_price"), r.get("prev_price")
            if o and p:
                rows.append({"c": code, "n": names.get(code, ""),
                             "open_pct": round((o / p - 1) * 100, 2),
                             "last_pct": (round((r["last_price"] / p - 1) * 100, 2)
                                          if r.get("last_price") else None)})
        return rows
    except Exception as e:  # noqa: BLE001 - 辅助面板数据，任何异常都不拖垮采集主流程
        LOG.warning(f"sector 失败（忽略）: {str(e)[:150]}")
        return None


# 2026-09-02 实测：09:25:10 终态时刻上游指数快照的 open 字段尚未就绪，
# 无重试导致 sector.json 整天停在上一交易日。open/prev 是日K属性，
# 稍后必出现，故退避重试；总等待封顶约 7 分钟，失败仍不阻塞主流程。
SECTOR_RETRY_WAITS = (60, 180, 300)


def fetch_sector(date8):
    """板块竞价强度：同花顺一级行业指数（881xxx）开盘缺口。

    指数开盘价由全部成分股集合竞价撮合而来，(open-prev)/prev 就是「板块内所有票」
    加权后的定盘口径，不是观察池那 108 只的有偏样本。
    且 open/prev 是日K属性：盘后补抓拿到的仍是今日开盘缺口，不会像个股竞价过程值
    那样被 09:25 冻结。失败不阻塞主流程；定盘时刻 open 未就绪时退避重试。
    """
    rows = _sector_rows()
    attempt = 0
    while not rows and attempt < len(SECTOR_RETRY_WAITS):
        LOG.warning(f"sector: 无有效行，{SECTOR_RETRY_WAITS[attempt]}s 后重试"
              f"（{attempt + 1}/{len(SECTOR_RETRY_WAITS)}）")
        time.sleep(SECTOR_RETRY_WAITS[attempt])
        attempt += 1
        rows = _sector_rows() or []
    if not rows:
        LOG.warning("sector: 重试后仍无有效行（不写盘），盘后手动补抓可恢复")
        return
    C.save_json("sector.json", {"date": date8,
                                "fetched_at": _now(), "rows": rows})
    LOG.info(f"sector: {len(rows)} 个一级行业开盘缺口"
          + (f"（第 {attempt + 1} 次尝试）" if attempt else ""))


def _pre_market():
    """是否在 09:15 前补抓：此时接口返回的是上一交易日终态，需诚实标注。"""
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min < 9 * 60 + 15


def _round_meta(rnd):
    """轮次的轻量元数据（不带 items）。"""
    return {"ts": rnd.get("ts"), "label": rnd.get("label"), "stage": rnd.get("stage"),
            "count": rnd.get("count"), "codes_total": rnd.get("codes_total"),
            "in_window": round_in_window(rnd), "errors": len(rnd.get("errors") or []),
            "phase": rnd.get("phase"), "data_status": rnd.get("data_status")}


def append_series(date8, rnd):
    """把一轮（live 或终态）并到当日 series.json，封顶保留 SERIES_KEEP_ROUNDS 轮。

    统一走这里：手动补抓也要有时序可查，否则曲线弹窗永远「当日尚无轮次」。
    但窗口外的补抓轮只保留最新一条：它们数值全等（都是定盘冻结值），
    累计三四条会被前端画成一条「平的假曲线」。
    同时写 rounds_meta.json（同序同过滤，但不含 items）：体检面板要画逐轮覆盖条，
    不该为此去读几百 KB 的 series.json。"""
    series = C.load_json("series.json") or {}
    if series.get("date") != date8 or not isinstance(series.get("rounds"), list):
        series = {"date": date8, "rounds": []}
    rounds = series["rounds"]
    if not round_in_window(rnd):
        rounds = [r for r in rounds if round_in_window(r)]
    rounds = (rounds + [rnd])[-C.SERIES_KEEP_ROUNDS:]
    C.save_json("series.json", {"date": date8, "rounds": rounds})
    C.save_json("rounds_meta.json", {"date": date8, "rounds": [_round_meta(r) for r in rounds]})


def _skip_status(date8, note):
    """不采集也要写状态：否则页面只能看到上一次成功记录的旧时间，以为今天采过了。"""
    C.save_status({"last_run": _now(), "date": date8, "rounds": 0, "ok": False, "note": note})


def run_once(date8):
    """手动/盘后补抓：final + benchmark（不进 live 轮询）。"""
    codes, meta = C.build_watchlist()
    if not codes:
        LOG.info("观察池为空（无自选且无复盘快照），跳过。")
        _skip_status(date8, "empty-pool：在 /auction 页「观察池」加代码，或先跑复盘抓取")
        return
    LOG.info(f"补抓 final：观察池 {meta['total']} 只（自选 {meta['user']} + 自动 {meta['auto']}）")
    rnd = collect_round(codes, "final", "manual")
    rnd.pop("label", None)
    # 09:25 前补抓时上游往往还没终态：data_status 不是 final 就标 provisional，前端据此提示
    provisional = (rnd.get("data_status") or "") != "final"
    C.save_json("final.json", {"date": date8, "fetched_at": _now(),
                               "mode": "manual", "pre_market": _pre_market(),
                               "provisional": provisional, "round": rnd})
    append_series(date8, dict(rnd, label="manual"))
    fetch_benchmark()
    AUI.maybe_refresh()   # 行业全量映射到期重建（失败不阻断），消灭「未分类」
    C.save_status({"last_run": _now(), "date": date8,
                   "rounds": 1, "mode": "manual", "ok": rnd["count"] > 0,
                   **({"note": "上游未返回终态（provisional）"} if provisional else {})})
    if provisional:
        LOG.warning("注意：上游 data_status 非 final，本次终态为盘中快照（provisional）。")
    fetch_sector(date8)   # 辅助面板：可能退避重试数分钟，放最后不拖住主流程状态


def _timeline(date8):
    trade = is_trade_today()
    if trade is False:
        LOG.info(f"{date8}: 非交易日，跳过。")
        _skip_status(date8, "non-trade-day")
        return
    codes, meta = C.build_watchlist()
    if not codes:
        LOG.info("观察池为空（无自选且无复盘快照），跳过。")
        _skip_status(date8, "empty-pool：在 /auction 页「观察池」加代码，或先跑复盘抓取")
        return
    LOG.info(f"== 竞价采集 {date8}：观察池 {meta['total']} 只"
          f"（自选 {meta['user']} + 自动 {meta['auto']}）==")

    # live 轮询：固定 30s 时点，错过的时点跳过（启动晚/上轮超时不追补）
    n_live = 0
    start, end = _today_at(C.LIVE_START), _today_at(C.LIVE_END)
    k = 0
    while True:
        b = start + k * C.INTERVAL
        k += 1
        if b > end:
            break
        # 睡到点为止（分片睡保留对时钟/锁变的响应）：之前只睡 min(wait, 30)，
        # 09:14 启动时第一轮会在 09:14:30 提前抓（竞价尚未开始，轻易拿到上一日口径）
        while True:
            delta = b - time.time()
            if delta <= 0:
                break
            time.sleep(min(delta, 5))
        if time.time() - b > C.MISS_GRACE:
            continue
        label = time.strftime("%H:%M:%S", time.localtime(b))
        rnd = collect_round(codes, "live", label)
        n_live += 1
        append_series(date8, rnd)
        C.save_json("live.json", {"date": date8, "updated_at": _now(),
                                  "round": rnd, "codes_total": len(codes)})
    C.save_status({"last_run": _now(), "date": date8, "rounds": n_live, "mode": "timeline",
                   "ok": n_live > 0,
                   **({"note": "窗口内 0 轮：启动晚于 09:15 或全程超窗（只有定盘快照，画不出竞价过程）"}
                      if n_live == 0 else {})})

    # 终态 09:25:10
    final_at = _today_at(C.FINAL_AT)
    # 错过时点的补跑（机器睡着/StartWhenAvailable 追跑）只能拿到定盘冻结值，
    # 不能再挂 "09:25:10" 这个假时点标签，否则 series 里会出现「标着 09:25 实为 13:01」的轮次。
    late_final = time.time() > final_at + C.MISS_GRACE
    if time.time() < final_at:
        time.sleep(final_at - time.time())
    fin_label = "manual" if late_final else "09:25:10"
    rnd = collect_round(codes, "final", fin_label)
    provisional = (rnd.get("data_status") or "") != "final"
    rnd.pop("label", None)
    C.save_json("final.json", {"date": date8, "fetched_at": _now(),
                               "mode": "timeline", "provisional": provisional, "round": rnd})
    append_series(date8, dict(rnd, label=fin_label))
    fetch_benchmark()
    AUI.maybe_refresh()   # 同上：终态路径顺带维护行业映射
    _notes = []
    if late_final:
        _notes.append("终态为窗口外补跑快照（不在 09:25:10 时点）")
    if provisional:
        _notes.append("上游未返回终态（provisional）")
    if not n_live:
        _notes.append("窗口内 0 轮，只有定盘快照")
    C.save_status({"last_run": _now(), "date": date8,
                   "rounds": n_live + 1, "mode": "timeline", "ok": rnd["count"] > 0,
                   **({"note": "；".join(_notes)} if _notes else {})})
    LOG.info("== 竞价采集完成 ==")
    fetch_sector(date8)   # 辅助面板：定盘时刻 open 可能未就绪，退避重试放最后不拖住状态


def run_timeline(once=False):
    date8 = datetime.date.today().strftime("%Y%m%d")
    lock = lockutil.acquire(C.LOCK_PATH)
    if lock is None:
        LOG.info("已有竞价采集在运行（锁 .status/fetch-auction.lock），本次跳过。")
        return
    try:
        if once:
            run_once(date8)
        else:
            _timeline(date8)
    finally:
        lockutil.release(lock)


def main():
    ap = argparse.ArgumentParser(description="实时竞价采集器")
    ap.add_argument("--once", action="store_true",
                    help="立即补抓 final+基准（盘后/手动），不进 live 轮询")
    args = ap.parse_args()
    run_timeline(once=args.once)


if __name__ == "__main__":
    try:  # 重定向到日志文件时固定用 UTF-8，与 auction_task.bat 的 PYTHONIOENCODING 一致
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
