# -*- coding: utf-8 -*-
"""美股收盘后复盘完成链（C6 时序改造，2026-09-09）。

T 日复盘的完成时点从 T 日 17:05 推迟到 T+1 美股收盘后 1 小时内（北京时间约
04:20 美夏令时 / 05:20 美冬令时），使 T 日备选池生成时能看到「隔夜美股」完整
场次，环境分档含 C6 隔夜美股闸门（纳指/标普 隔夜 ≤-2.0% 强制防守）。

时序（计划任务 arecap-usclose-fetch 每日 04:05 触发本脚本）：
  1. 等待到最近的美股收盘 +20 分钟（美东 16:20，zoneinfo 自动处理夏令时；
     +20 分钟同时保证 us_market 的「未收盘 bar 剔除」守卫（16:15 ET）已放行）；
  2. hithink-finance data sync（研究库增量，非阻塞）：17:05 的 sync 与上游
     「T 日 release ≈17:11 发布」存在竞态（0911/0912/0913 压线赶上，0914 未
     赶上→SKIP），而本链的池/验证全部依赖 T 日线——重建前必须再同步一次；
  3. python backend/us_market.py                  # 抓隔夜美股 + 重建 factors.json
     （失败按 US_FETCH_RETRY_S 重试一次；仍失败即中止本次终版结算，防 C6 闸门用过期
     factors——缺口留给下一次链）
  4. 缺口兜底（2026-10-21 事故新增）：近 GAP_LOOKBACK 个 A 股交易日里，凡
     「隔夜场次已收盘（us_market.row_freshness=True）但备选池仍是 pending 占位」
     者逐日补跑——本链被中断 / 04:05 被错过时，被跳过的那个 T 次日不会再被算到
     （次日链只算次日），这道扫描保证「漏跑的池」被下一次链自动补出；
     上一交易日无快照 → 告警（17:05 采集失败，白天人工补），但仍继续扫描更早缺口。
  5. 每个缺口 T：python backend/recap/speculate.py --date T   # 备选池终版 + 验证归因
  6. python backend/recap/check_snapshot.py data/recap/<T>.<ext>
  7. python backend/derive.py

全部步骤落盘 .status/logs/usclose.log；A 股节假日（无 T 快照）只跳过复盘步骤，
美股数据仍照常更新。非投资建议，数据规则化处理。
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOG = os.path.join(ROOT, ".status", "logs", "usclose.log")

US_CLOSE_ET = (16, 20)     # 美东收盘 16:00 + 20 分钟缓冲（数据落定 + 守卫放行）
MAX_WAIT_MIN = 120         # 等待上限（异常早触发时兜底）
GAP_LOOKBACK = 6           # 缺口兜底回溯的 A 股交易日数（覆盖一周内任何一次漏跑）
US_FETCH_RETRY_S = 90      # us_market 失败后的重试等待（秒）：开机触发时网络常未就绪


def _log(msg):
    line = f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def wait_us_close():
    """若美东尚未到当日 16:20，睡到该时刻（DST 由 zoneinfo 处理）。"""
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/New_York"))
        target = now.replace(hour=US_CLOSE_ET[0], minute=US_CLOSE_ET[1],
                             second=0, microsecond=0)
        wait_s = (target - now).total_seconds()
        if 0 < wait_s <= MAX_WAIT_MIN * 60:
            _log(f"wait {wait_s/60:.1f} min until US close+20m ({target.isoformat()} ET)")
            time.sleep(wait_s)
        elif wait_s > MAX_WAIT_MIN * 60:
            _log(f"[warn] wait {wait_s/60:.0f} min exceeds cap, proceed now (data may lag)")
    except Exception as e:  # noqa: BLE001 - 时区不可用时不等待（17:05 兜底链仍在）
        _log(f"[warn] zoneinfo unavailable ({type(e).__name__}), skip wait")


def sync_research_db() -> bool:
    """研究库（hithink 本地 DuckDB）增量同步（非阻塞）。

    根因（0914 实例）：上游「T 日 release ≈17:11 发布」晚于 17:05 sync → 当日
    SKIP；次日凌晨 04:21 重建终版池/验证时本地库整层缺 T 日线——低吸组空仓、
    偏离/量比空、验证全标「停牌/数据缺失」。这里在重建前补一次同步（此时上游
    必已发布），失败仅 [warn]：缺口由 spec_duckdb 探测落盘 + 东财备源兜底。"""
    sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))
    try:
        import ht  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        _log(f"[warn] hithink CLI 不可用: {type(e).__name__}: {str(e)[:150]}")
        return False
    # 默认 1GiB 上限在本机会让 sync 提交失败（README「重建或换机时记得带上」）
    os.environ.setdefault("HITHINK_FINANCE_DUCKDB_MEMORY_LIMIT", "4GiB")
    try:
        d = ht.ht("data", "sync", timeout=600) or {}
        _log(f"sync ok: decision={(d or {}).get('decision')} "
             f"release={(d or {}).get('release_id')}")
        return True
    except Exception as e:  # noqa: BLE001 - 非阻塞：缺口由探测+备源兜底
        _log(f"[warn] hithink data sync failed (non-blocking): "
             f"{type(e).__name__}: {str(e)[:200]}")
        return False


def prev_ashare_trade_date(dates=None) -> str | None:
    """上一个 A 股交易日（ISO，相对北京时间今天）；来源 us_market 日历单一来源。

    dates 可传入已算好的「今日之前」交易日升序列表（省一次日历解析，见 main）。
    """
    if dates is None:
        sys.path.insert(0, os.path.join(ROOT, "backend"))
        import us_market  # noqa: PLC0415
        today = _dt.date.today().isoformat()
        dates = [d for d in us_market.load_ashare_dates() if d < today]
    return dates[-1] if dates else None


def _snapio():
    """懒加载快照读写单一入口（backend/recap/snapio.py，兼容 .json / .json.gz）。"""
    p = os.path.join(ROOT, "backend", "recap")
    if p not in sys.path:
        sys.path.insert(0, p)
    import snapio  # noqa: PLC0415
    return snapio


def _t8(iso: str) -> str:
    """ISO 日期 -> 8 位快照日键。"""
    return iso.replace("-", "")


def _rel_snap_path(date8: str) -> str:
    """给 check_snapshot 的相对路径（沿用原链 data/recap/<name> 形态，兼容 .gz）。"""
    io = _snapio()
    return "data/recap/" + os.path.basename(io.snap_path(io.RECAP_DATA, date8))


def _snap_exists(date8: str) -> bool:
    io = _snapio()
    return os.path.exists(io.snap_path(io.RECAP_DATA, date8))


def session_closed_target(iso: str) -> bool:
    """该 A 股日期的「隔夜美股场次」是否已收盘（=该日池可终版化）。

    复用守卫单一来源 us_market.row_freshness：场次未收盘时补跑只会再产出一个
    pending 占位，故先过这道闸。缺行/读盘异常按 False（宁可不补、不误覆盖）。
    """
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import us_market  # noqa: PLC0415
    try:
        return bool(us_market.row_freshness(iso)[1])
    except Exception:  # noqa: BLE001 - 读盘异常按“不可终版化”处理
        return False


def pool_not_final(iso: str) -> bool:
    """该日（ISO 交易日）快照的备选池是否尚未终版（需补跑）。

    入参为 ISO 日期（与 session_closed_target 同口径，_pick_gaps 的两个谓词统一），
    内部转 8 位快照日键。终版口径 = 有 pool、非 pending 占位、且带口径 note
    （spec_pool 写入的「…口径（…）」首段）——真池三者同时成立。反之（pending 占位 /
    pool 段缺失 / error 池无 note）都算未终版、纳入补跑（与改造前「无条件重算 T 日」
    同覆盖，且历史真池会被 speculate 既有的口径守卫挡下，不会被覆盖）。快照缺失/
    读不到一律 False——补跑无意义，由调用方 _snap_exists 另行告警。
    """
    try:
        doc = _snapio().load(_t8(iso))
    except Exception:  # noqa: BLE001 - 读异常按“不需补跑”处理
        return False
    if doc is None:
        return False
    mod = ((doc.get("modules") or {}).get("speculation") or {}).get("data") or {}
    pool = mod.get("pool") or {}
    if not pool:
        return True
    return pool.get("stage") == "pending" or not str(pool.get("note") or "").strip()


def _pick_gaps(candidates, eligible, pending, limit=GAP_LOOKBACK):
    """纯函数：挑出「终版前置条件已满足、但备选池仍是 pending 占位」的日期。

    candidates 升序 ISO 交易日串（新在末——逐日补跑保验证/优化器指标落账时序）；
    eligible(day) 该日隔夜场次已定、可终版化；pending(day) 该日池仍为 pending 占位。
    仅回溯最近 limit 个交易日（防机器久停后无界补跑）。返回升序子集，不改入参。
    """
    return [d for d in candidates[-limit:] if eligible(d) and pending(d)]


def run_step(cmd: list[str]) -> bool:
    _log("RUN " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        _log("  " + out.replace("\n", "\n  ")[-1500:])
    if err:
        _log("  [stderr] " + err.replace("\n", "\n  ")[-800:])
    _log(f"  -> rc={r.returncode}")
    return r.returncode == 0


def run_us_fetch() -> bool:
    """抓隔夜美股 + 重建 factors.json；失败后按 US_FETCH_RETRY_S 重试一次。

    开机触发（BootTrigger）时网络往往尚未就绪，单次失败即中止会让该日池顺延到
    下一次链；这里给一次短重试兜住这种瞬时失败。仍失败则返回 False，由 main
    中止本次终版结算（防 C6 隔夜美股闸门用到过期 factors）。
    """
    cmd = [sys.executable, os.path.join("backend", "us_market.py")]
    if run_step(cmd):
        return True
    _log(f"[warn] us_market fetch failed; retry in {US_FETCH_RETRY_S}s "
         f"(boot-time network may not be ready)")
    time.sleep(US_FETCH_RETRY_S)
    return run_step(cmd)


def _notify(title, text, key, min_hours=12):
    """链路失败推送（0915 起）：尽力而为，绝不反过来打断链路。key 节流防重复。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "backend"))
        import notify  # noqa: PLC0415
        notify.send(title, text, key=key, min_interval_hours=min_hours)
    except Exception as e:  # noqa: BLE001
        _log(f"[warn] notify failed: {type(e).__name__}: {str(e)[:120]}")


def main():
    _log("==== us-close recap chain start ====")
    sync_research_db()   # 先补研究库（利用等待窗口），T 日线是后面池/验证的硬依赖
    wait_us_close()

    ok_us = run_us_fetch()
    if not ok_us:
        # 加严（2026-10-21）：抓取（含一次短重试）仍失败即中止本次终版结算。旧版只在
        # [warn] 后继续——一旦 factors 陈旧（如开机触发时网络未就绪），row_freshness
        # 仍会因「场次已按墙钟收盘」放行，从而把池按过期隔夜美股数据标成终版
        # （C6 闸门取值偏）。缺口留给下一次链（04:05 / 开机触发 / 缺口兜底）自动补。
        _log("[warn] us_market fetch failed (after retry); skip finalization this run")
        text = ("us_close_task 中 us_market.py 失败，factors.json 沿用旧值——"
                "为避免 C6 隔夜美股闸门用到过期数据，本次跳过终版结算，"
                "缺口留给下一次链自动补跑。白天可在看板手动重抓。")
        try:   # 附最新失败现场采样（netguard.capture_sample 落盘），推送与明细不互堵
            sys.path.insert(0, os.path.join(ROOT, "backend"))
            import netguard  # noqa: PLC0415
            note = netguard.latest_sample_note()
            if note:
                text += f"\n{note}\n应急取数与采样判读见 docs/运维手册-上游应急取数.md"
        except Exception as e:  # noqa: BLE001 - 采样摘要是诊断副线，拿不到照常推送
            _log(f"[warn] sample note unavailable: {type(e).__name__}")
        _notify("隔夜美股抓取失败", text, key="chain:us-failed")
        return 1

    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import us_market  # noqa: PLC0415
    today = _dt.date.today().isoformat()
    dates = [d for d in us_market.load_ashare_dates() if d < today]   # 升序（新在末）
    t_iso = prev_ashare_trade_date(dates)
    if not t_iso:
        _log("[abort] 无法确定上一个 A 股交易日（日历缺失）")
        _notify("复盘完成链中止", "无法确定上一个 A 股交易日（日历缺失）。",
                key="chain:abort")
        return 1
    t8 = _t8(t_iso)
    if not _snap_exists(t8):
        # 无快照 = 17:05 采集链失败：美股已更新，本日复盘跳过。t8 取自交易日历
        # （prev_ashare_trade_date），按定义是交易日——快照缺失即真失败，推送。
        # 不早退：更早的 pending 缺口仍由下面的缺口扫描兜底补出。
        _log(f"[skip] {t8} 无复盘快照（17:05 采集链失败），本日终版跳过")
        _notify("复盘完成链跳过",
                f"{t8} 无复盘快照——17:05 采集链很可能失败了，白天请人工补抓。",
                key="chain:snap-missing")

    # 缺口兜底（2026-10-21 事故）：链被中断 / 04:05 被错过时，被跳过的那个 T 次日不会
    # 再被算到（次日链只算次日）——这里把「场次已收盘但池仍未终版」的历史日期逐日补跑，
    # 保证漏跑的池必被下一次链自动补出。
    gaps = _pick_gaps(dates, session_closed_target, pool_not_final)
    window = dates[-GAP_LOOKBACK:]
    _log(f"缺口扫描（近 {len(window)} 交易日 {window[0] if window else '-'}..{t_iso}）："
         f"待补 {len(gaps)} 日 {gaps or '-'}")
    if len(gaps) >= GAP_LOOKBACK:
        _log(f"[warn] 缺口触达回溯上限 {GAP_LOOKBACK} 日，久停可能仍有更早缺口未补")

    steps = []
    for d in gaps:
        d8 = _t8(d)
        steps.append([sys.executable, os.path.join("backend", "recap", "speculate.py"),
                      "--date", d8])
        steps.append([sys.executable, os.path.join("backend", "recap", "check_snapshot.py"),
                      _rel_snap_path(d8)])
    steps.append([sys.executable, os.path.join("backend", "derive.py")])
    rcs = [run_step(c) for c in steps]
    _log(f"==== us-close recap chain end (us={ok_us} gaps={len(gaps)} steps={rcs}) ====")
    if not all(rcs):
        bad = [os.path.basename(c[-1]) for c, rc in zip(steps, rcs) if not rc]
        _notify("复盘完成链步骤失败",
                f"缺口 {gaps or t8} 链中失败步骤：{', '.join(bad)}"
                f"（明细见 .status/logs/usclose.log）",
                key="chain:steps-failed")
    return 0 if all(rcs) else 1


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(main())
