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
  4. T = 上一个 A 股交易日；T 无快照 → 告警退出（17:05 采集失败，白天人工补）
  5. python backend/recap/speculate.py --date T   # 备选池（含闸门）+ T-1 验证归因
  6. python backend/recap/check_snapshot.py data/recap/<T>.json
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


def prev_ashare_trade_date() -> str | None:
    """上一个 A 股交易日（ISO，相对北京时间今天）；来源 us_market 日历单一来源。"""
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import us_market  # noqa: PLC0415
    today = _dt.date.today().isoformat()
    dates = [d for d in us_market.load_ashare_dates() if d < today]
    return dates[-1] if dates else None


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

    ok_us = run_step([sys.executable, os.path.join("backend", "us_market.py")])
    if not ok_us:
        _log("[warn] us_market fetch failed; proceed with existing factors.json")
        _notify("隔夜美股抓取失败",
                "us_close_task 中 us_market.py 失败，factors.json 沿用旧值——"
                "环境闸门可能用到过期数据。白天可在看板手动重抓。",
                key="chain:us-failed")

    t_iso = prev_ashare_trade_date()
    if not t_iso:
        _log("[abort] 无法确定上一个 A 股交易日（日历缺失）")
        _notify("复盘完成链中止", "无法确定上一个 A 股交易日（日历缺失）。",
                key="chain:abort")
        return 1
    t8 = t_iso.replace("-", "")
    snap = os.path.join(ROOT, "data", "recap", f"{t8}.json")
    snap_gz = snap + ".gz"
    if not (os.path.exists(snap) or os.path.exists(snap_gz)):
        # 无快照 = 17:05 采集链失败：美股已更新，复盘跳过。t8 取自交易日历
        # （prev_ashare_trade_date），按定义是交易日——快照缺失即真失败，推送。
        _log(f"[skip] {t8} 无复盘快照（17:05 采集链失败），仅完成美股更新")
        _notify("复盘完成链跳过",
                f"{t8} 无复盘快照——17:05 采集链很可能失败了，白天请人工补抓。",
                key="chain:snap-missing")
        return 0 if ok_us else 1

    steps = [
        [sys.executable, os.path.join("backend", "recap", "speculate.py"), "--date", t8],
        [sys.executable, os.path.join("backend", "recap", "check_snapshot.py"), f"data/recap/{t8}.json"],
        [sys.executable, os.path.join("backend", "derive.py")],
    ]
    rcs = [run_step(c) for c in steps]
    _log(f"==== us-close recap chain end (us={ok_us} steps={rcs}) ====")
    if not all(rcs):
        bad = [os.path.basename(c[-1]) for c, rc in zip(steps, rcs) if not rc]
        _notify("复盘完成链步骤失败",
                f"{t8} 链中失败步骤：{', '.join(bad)}（明细见 .status/logs/usclose.log）",
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
