# -*- coding: utf-8 -*-
"""轮动采集器（同花顺口径 · 2026-09-01 起整站板块统一为同花顺一级行业 90 个）。

背景：轮动页历史上用东财 trends2 拉板块分时（收盘后一次可得全天 241 个 1 分钟点）。
hithink 侧没有分钟级指数接口（capabilities 实测：index.history 只有日线，
index.snapshot 是实时快照批量可查），因此换口径后分时曲线改为**盘中逐分钟轮询
快照累积**：一次批量请求拿全 90 个 881xxx 一级行业指数，按采样时刻落成时点。

诚实边界（换口径的既定代价，README 已写明）：
  - 盘中漏采的分钟不可回补（东财旧口径可以盘后追溯）；coverage.minutes 会如实反映。
  - 盘外补抓只保留一个终态定格点（15:00），不伪造分钟序列（同竞价页口径）。
  - 东财历史数据在 data/rotation/daily_legacy_eastmoney/ 归档，派生层不再混算。

用法:
    python ths_collect.py                  # 循环模式：交易时段内每 60s 采一轮，收盘退出
    python ths_collect.py --interval 120   # 调整采样间隔（秒）
    python ths_collect.py --once           # 只采一轮（配合计划任务或手动定格）
    python ths_collect.py --date 2026-09-02  # 指定写入日期（测试用，默认今天）

输出（schema 与旧 fetch_day.py 兼容，derive.py 零改动）：
    data/rotation/intraday/<date>.raw.json   逐轮原始快照（累积日志，amounts 差值来源）
    data/rotation/daily/<date>.json          当日分时（服务端/派生层消费）
    data/rotation/boards.json                板块池清单（同花顺一级 881xxx，单一来源）
    .status/rotation.json                    采集状态（/api/health 数据管家消费）
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend", "recap"))

import ht          # noqa: E402  hithink CLI 封装（与复盘/竞价共用同一客户端）
import lockutil    # noqa: E402
import trade_cal   # noqa: E402  交易日历判定单一来源（backend/trade_cal.py）
import logutil     # noqa: E402  统一 logging（时间戳/级别）

LOG = logutil.get_logger("ak.rotation")

DATA_DIR = os.path.join(PROJECT_ROOT, "data", "rotation")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
RAW_DIR = os.path.join(DATA_DIR, "intraday")
BOARDS_FILE = os.path.join(DATA_DIR, "boards.json")
CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "cache")
LOCK_PATH = os.path.join(PROJECT_ROOT, ".status", "fetch-rotation.lock")
STATUS_PATH = os.path.join(PROJECT_ROOT, ".status", "rotation.json")

# 交易时段（分钟）：与旧口径一致，分时从 09:30 开始（竞价过程归竞价页管）；
# 数据只到 15:00（用户指定）：15:00 快照即收盘值，收盘后不再落点（2026-09-03 前曾多采 15:01/15:02 冗余点）
AM_OPEN, AM_CLOSE = 9 * 60 + 30, 11 * 60 + 30
PM_OPEN, PM_CLOSE = 13 * 60, 15 * 60


def in_trading_minutes(hm):
    return AM_OPEN <= hm <= AM_CLOSE or PM_OPEN <= hm <= PM_CLOSE


def is_trade_today():
    """hithink 交易日历；拿不到结论返回 None（按交易日继续）。

    判定单一来源 backend/trade_cal.py（两侧只取数字比、schema 变了返回 None
    继续采；竞价采集器曾因格式不一致把每个交易日判成非交易日、静默停摆一个月）。
    """
    return trade_cal.is_trade_today()


# ---------------- 板块池（单一来源：同花顺行业目录 881xxx = 一级 90 个） ----------------

def load_board_pool(refresh=False):
    """返回 [(thscode, name)]，并同步维护 boards.json。

    防呆：一级数量 ≠ 90 附近（上游目录结构变了）时拒绝刷新，退回已有清单——
    宁可用昨天的 90 个继续采，也不要静默换成混层目录（竞价行业映射同口径教训）。
    """
    def _read_saved():
        try:
            with open(BOARDS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("src") == "ths" and len(d.get("industry") or []) >= 80:
                return [(x["code"], x["name"]) for x in d["industry"]]
        except Exception:
            pass
        return None

    if not refresh:
        saved = _read_saved()
        if saved:
            return saved
    try:
        cats = ht.catalog("industry", cache_dir=CACHE_DIR, cache_days=7)
        pool = [(c["thscode"], c.get("name") or c["thscode"])
                for c in cats if str(c.get("thscode", "")).startswith("881")]
        if len(pool) < 80:
            raise RuntimeError(f"目录一级行业仅 {len(pool)} 个（预期约 90），上游结构可能变了")
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = BOARDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"src": "ths", "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "industry": [{"code": c, "name": n} for c, n in pool],
                       "concept": []}, f, ensure_ascii=False)
        os.replace(tmp, BOARDS_FILE)
        return pool
    except Exception as e:  # noqa: BLE001
        saved = _read_saved()
        if saved:
            LOG.warning(f"!! 板块池刷新失败（{e}），沿用已有 {len(saved)} 个")
            return saved
        raise


# ---------------- 原始逐轮日志（累积 → 重建 daily 文件） ----------------

def raw_path(date_s):
    return os.path.join(RAW_DIR, f"{date_s}.raw.json")


def load_raw(date_s):
    try:
        with open(raw_path(date_s), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_raw(raw):
    os.makedirs(RAW_DIR, exist_ok=True)
    tmp = raw_path(raw["date"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    os.replace(tmp, raw_path(raw["date"]))


def sample_round(pool, raw, stamp_hm, in_window):
    """拉一轮 90 行业快照，追加到 raw。返回 (ok_codes, failed)。

    raw["rounds"]: [{t, w(in_window), s:{code:[price,pct,cum_turnover]}}]
    pct=(last-prev)/prev；cum 为当日累计成交额，重建 daily 时按相邻轮差分。
    """
    codes = [c for c, _ in pool]
    names = {c: n for c, n in pool}
    snap = ht.index_snapshot_batches(codes)
    sel = {}
    failed = []
    for code in codes:
        row = snap.get(code)
        try:
            last = row.get("last_price")
            prev = row.get("prev_price")
            if last is None or not prev:
                raise ValueError("no price")
            pct = round((last / prev - 1) * 100, 2)
            sel[code] = [last, pct, row.get("turnover") or 0]
        except Exception as e:  # noqa: BLE001
            failed.append({"code": code, "name": names.get(code, code),
                           "error": str(e)[:80]})
    if not sel:
        return [], failed
    raw["rounds"].append({"t": stamp_hm, "w": bool(in_window), "s": sel})
    save_raw(raw)
    return list(sel.keys()), failed


def build_daily(pool, raw, note_extra=None):
    """raw 累积日志 → 与旧 fetch_day 输出兼容的 daily 文件 dict。

    时间轴 = 各板块出现时刻的交集（同旧口径的分钟缺口裁剪）；
    amounts = 相邻公共时间点的成交额差分（首点为累计值）；
    窗口外轮次默认丢弃，仅当全天没有任何窗口内轮时保留最新一条作终态定格。
    """
    win = [r for r in raw["rounds"] if r.get("w")]
    used = list(win)
    catchup_note = None
    if not win:
        # 盘外补抓：只留最后一轮当定格快照，不冒充分钟序列
        used = raw["rounds"][-1:] if raw["rounds"] else []
        if used:
            catchup_note = "窗口外补抓：仅终态定格 1 个点，当日盘中未逐轮采集"
    else:
        # 盘外定格点（11:30 午休 / 15:00 收盘，价格属该收盘时刻）是真实数据点：
        # 窗口内恰好没采到该时点时并入（盘中循环 14:45 死亡后、17 点任务补的 15:00
        # 定格曾被整条丢弃，daily 尾部停在 14:45）；窗口内已采到则不重复
        have = {r["t"] for r in used}
        for stamp in ("11:30", "15:00"):
            if stamp in have:
                continue
            frz = [r for r in raw["rounds"] if not r.get("w") and r["t"] == stamp]
            if frz:
                used = sorted(used + [frz[-1]], key=lambda r: r["t"])
    names = dict(pool)
    per = {}   # code -> {t: (price,pct,cum)}
    for r in used:
        for code, (price, pct, cum) in r["s"].items():
            per.setdefault(code, {})[r["t"]] = (price, pct, cum)
    ok_codes = [c for c, _ in pool if c in per]
    times = []
    if ok_codes:
        common = set(per[ok_codes[0]])
        for c in ok_codes[1:]:
            common &= set(per[c])
        times = sorted(common)
    boards, series = [], {}
    for code in ok_codes:
        pts = [per[code][t] for t in times]
        amounts = []
        prev_cum = None
        for (_p, _pc, cum) in pts:
            if prev_cum is None:
                amounts.append(float(cum))
            else:
                amounts.append(max(0.0, float(cum) - float(prev_cum)))
            prev_cum = cum
        boards.append({"code": code, "name": names.get(code, code),
                       "type": "industry", "mcap": 0})
        series[code] = {"name": names.get(code, code), "type": "industry",
                        "times": times,
                        "pcts": [p[1] for p in pts],
                        "prices": [round(p[0], 2) for p in pts],
                        "amounts": amounts}
    out = {"date": raw["date"], "src": "ths",
           "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "boards": boards, "series": series,
           "failed": raw.get("last_failed") or [],
           "times": times}
    trimmed = None
    all_ts = {r["t"] for r in used}
    if times and len(all_ts) > len(times):
        trimmed = f"交集裁剪 {len(all_ts) - len(times)} 分钟（个别板块快照缺口）"
        out["trimmed"] = trimmed
    _t = times
    coverage = {
        "minutes": len(_t),
        "last_time": _t[-1] if _t else None,
        "complete": len(_t) >= 235 and bool(_t) and _t[-1] == "15:00",
        "trimmed_note": trimmed,
        "source": "同花顺一级行业指数快照逐分钟轮询（index snapshot）",
    }
    if catchup_note:
        coverage["note"] = catchup_note
    out["coverage"] = coverage
    if note_extra:
        out["note"] = note_extra
    return out


def write_daily(out):
    os.makedirs(DAILY_DIR, exist_ok=True)
    path = os.path.join(DAILY_DIR, f"{out['date']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def write_status(out, failed):
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        json.dump({
            "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": out["date"], "src": "ths",
            "boards": len(out.get("boards") or []),
            "times": len(out.get("times") or []),
            "failed": [[f_.get("code"), f_.get("error")] for f_ in (failed or [])][:20],
            "exit": 0,
        }, open(STATUS_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        LOG.info(f"  状态写入失败: {e}")


def prune(dir_path, keep_days, suffix=".json"):
    """按文件名前 10 位日期清理超期文件（与旧 fetch_day --keep-days 同口径）。"""
    if not os.path.isdir(dir_path):
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    for fn in os.listdir(dir_path):
        stem = fn.replace(".raw" + suffix, "") if suffix == ".json" else fn
        try:
            d = datetime.date.fromisoformat(stem[:10])
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.remove(os.path.join(dir_path, fn))
            except OSError:
                pass


# ---------------- 主流程 ----------------

def run_derive():
    """盘中每轮采集后刷新派生面板（强度矩阵/形态/竞赛等）。
    derive 自带增量门（源文件不比面板新则 0.2s 内直接返回），全量重算也只需 ~0.2s，
    每 60s 一轮完全负担得起；失败不致命，只告警。历史背景：2026-09-03 前 derive 只在收盘后跑，
    盘中强度矩阵/形态/相关性全部冻结，用户报「矩阵不更新」。"""
    derive_py = os.path.join(PROJECT_ROOT, "backend", "derive.py")
    try:
        subprocess.run([sys.executable, derive_py], cwd=PROJECT_ROOT,
                       timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as e:  # noqa: BLE001
        LOG.warning(f"derive 刷新失败（不致命）: {e}")


def run(args):
    lock = lockutil.acquire(LOCK_PATH)
    if lock is None:
        LOG.info(f"{time.strftime('%F %T')}: 已有轮动采集在运行（锁 {LOCK_PATH}），本次跳过。")
        return 0
    try:
        return _run(args, lock)
    finally:
        lockutil.release(LOCK_PATH)


def _next_tick(now, interval):
    """下一次采样时刻：对齐到整分钟 :02（间隔取 60s 的倍数）。

    「采一轮 + 固定 sleep」会让每轮起点持续后漂（一轮请求耗时叠加在间隔上），
    漂移跨过分钟边界就整分钟跳过——09-03 13:58→14:00、09-04 13:51→13:53 等
    2 分钟小洞均此来源。对齐后 stamp 稳定落在每分钟 :02，只有单轮耗时超过
    约 58s（上游极慢）时才会丢分钟。"""
    mins = max(1, int(round(interval / 60)))
    cand = now.replace(second=2, microsecond=0) + datetime.timedelta(minutes=mins)
    if (cand - now).total_seconds() < 5:   # 刚好压线：再退一个节拍，避免亚 5 秒连采
        cand += datetime.timedelta(minutes=mins)
    return cand


def _run(args, lock):
    date_s = args.date or time.strftime("%Y-%m-%d")
    if not args.force and date_s == time.strftime("%Y-%m-%d"):
        wd = datetime.date.fromisoformat(date_s).weekday()
        if wd >= 5:
            LOG.info(f"{date_s}: 周末非交易日，跳过（--force 可强制）")
            return 0
        ok = is_trade_today()
        if ok is False:
            LOG.info(f"{date_s}: 交易日历判定为非交易日，跳过（--force 可强制）")
            return 0
    pool = load_board_pool(refresh=False)
    raw = load_raw(date_s) or {"date": date_s, "rounds": []}

    def one_round(in_window):
        now = datetime.datetime.now()
        stamp = now.strftime("%H:%M")
        if not in_window:
            hm = now.hour * 60 + now.minute
            if hm < AM_OPEN and date_s == time.strftime("%Y-%m-%d"):
                # 盘前快照是昨收值，拿它冒充今日定格是造假；直接拒采
                LOG.info(f"[{stamp}] 盘前非交易时段，今日尚无分时数据，不采样。")
                return None
            # 盘外定格点的价格属于上一收盘时刻，时间标签用收盘时刻而非补抓时刻
            stamp = "11:30" if AM_CLOSE < hm < PM_OPEN else "15:00"
            if any(r.get("w") and r.get("t") == stamp for r in raw["rounds"]):
                # 盘中已采到该时点（收盘定格轮本身就在窗口内）：不重复补抓，重建一次文件即退
                out = build_daily(pool, raw)
                write_daily(out)
                write_status(out, out.get("failed"))
                LOG.info(f"[{stamp}] 窗口内已有点位，定格补抓跳过。")
                return out
        if raw["rounds"] and raw["rounds"][-1]["t"] == stamp:
            raw["rounds"][-1] = {**raw["rounds"][-1], "w": bool(in_window)}
            stamp = (now + datetime.timedelta(seconds=1)).strftime("%H:%M")
        ok_codes, failed = [], []
        for attempt in (1, 2):
            try:
                ok_codes, failed = sample_round(pool, raw, stamp, in_window)
                if ok_codes:
                    break
                if attempt == 1:
                    # 整轮空=上游瞬时抖动：同一分钟内重试一次，别把抖动留成永久分钟洞
                    time.sleep(5)
            except Exception as e:  # noqa: BLE001 - 整轮失败：不写假点，如实记录
                LOG.warning(f"[{stamp}] 轮次失败（第 {attempt} 次）: {e}")
        if not ok_codes:
            return None
        raw["last_failed"] = [f for f in failed]
        out = build_daily(pool, raw)
        path = write_daily(out)
        write_status(out, failed)
        run_derive()   # 盘中面板随采集分钟级刷新（增量门兑底，无新数据时 0.2s 返回）
        LOG.info(f"[{stamp}] 采集 {len(ok_codes)}/{len(pool)} 板块 · "
              f"时间轴 {out['coverage']['minutes']} 点 -> {os.path.basename(path)}"
              + (f" · 失败 {len(failed)}" if failed else ""))
        return out

    hm = datetime.datetime.now().hour * 60 + datetime.datetime.now().minute
    if args.once:
        out = one_round(in_window=in_trading_minutes(hm))
        prune(DAILY_DIR, args.keep_days)
        prune(RAW_DIR, args.keep_days)
        return 0 if out else 1
    if not in_trading_minutes(hm):
        # 循环模式在盘外启动：先定格当前状态，再把控制权交回交易时段
        if hm > PM_CLOSE:
            one_round(in_window=False)
            prune(DAILY_DIR, args.keep_days)
            prune(RAW_DIR, args.keep_days)
            LOG.info("已收盘，定格退出。")
            return 0
        if AM_CLOSE < hm < PM_OPEN:
            one_round(in_window=False)   # 午间定格上午尾点（价格属 11:30）
            LOG.info("午间休市，等待 13:00 继续轮询…")
        else:
            LOG.info(f"当前 {hm//60:02d}:{hm%60:02d} 早于开盘，等待 09:30 开始轮询…")
        while True:
            now = datetime.datetime.now()
            target = PM_OPEN if AM_CLOSE < hm < PM_OPEN else AM_OPEN
            if now.hour * 60 + now.minute >= target:
                break
            time.sleep(20)

    LOG.info(f"轮动采集启动（同花顺一级 {len(pool)} 行业 · 每 {args.interval}s 一轮）")
    while True:
        now = datetime.datetime.now()
        hm = now.hour * 60 + now.minute
        if hm >= PM_CLOSE:
            # 收盘定格：数据到 15:00 为止。恰好 15:00 就盘中采；晚于 15:00 才醒来则走盘外逻辑定格成「15:00」点（含同名点位去重）
            one_round(in_window=(hm == PM_CLOSE))
            break
        if not in_trading_minutes(hm):
            time.sleep(30)
            continue
        one_round(in_window=True)
        # 心跳：长循环不能被 40 分钟 stale 判定抢锁
        try:
            os.utime(LOCK_PATH, None)
        except OSError:
            pass
        # 节拍对齐整分钟（见 _next_tick），固定 sleep 会漂移跳分钟
        time.sleep(max(5, (_next_tick(datetime.datetime.now(), args.interval)
                           - datetime.datetime.now()).total_seconds()))
    prune(DAILY_DIR, args.keep_days)
    prune(RAW_DIR, args.keep_days)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="轮动采集（同花顺一级行业指数快照轮询）")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒（默认 60）")
    ap.add_argument("--once", action="store_true", help="只采一轮")
    ap.add_argument("--date", default=None, help="写入日期 YYYY-MM-DD（默认今天，测试用）")
    ap.add_argument("--force", action="store_true", help="非交易日也采")
    ap.add_argument("--refresh-boards", action="store_true", help="强制刷新板块池")
    ap.add_argument("--keep-days", type=int, default=120)
    args = ap.parse_args()
    if args.refresh_boards:
        load_board_pool(refresh=True)
    sys.exit(run(args))
