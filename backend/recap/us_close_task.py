# -*- coding: utf-8 -*-
"""美股收盘后复盘完成链（C6 时序改造，2026-09-09）。

T 日复盘的完成时点从 T 日 17:05 推迟到 T+1 美股收盘后 1 小时内（北京时间约
04:20 美夏令时 / 05:20 美冬令时），使 T 日备选池生成时能看到「隔夜美股」完整
场次，环境分档含 C6 隔夜美股闸门（纳指/标普 隔夜 ≤-2.0% 强制防守）。

时序（计划任务 arecap-usclose-fetch 每日 04:05 触发本脚本）：
  1. 等待到最近的美股收盘 +20 分钟（美东 16:20，zoneinfo 自动处理夏令时；
     +20 分钟同时保证 us_market 的「未收盘 bar 剔除」守卫（16:15 ET）已放行）；
  2. python backend/us_market.py                  # 抓隔夜美股 + 重建 factors.json
  3. T = 上一个 A 股交易日；T 无快照 → 告警退出（17:05 采集失败，白天人工补）
  4. python backend/recap/speculate.py --date T   # 备选池（含闸门）+ T-1 验证归因
  5. python backend/recap/check_snapshot.py data/recap/<T>.json
  6. python backend/derive.py

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


def main():
    _log("==== us-close recap chain start ====")
    wait_us_close()

    ok_us = run_step([sys.executable, os.path.join("backend", "us_market.py")])
    if not ok_us:
        _log("[warn] us_market fetch failed; proceed with existing factors.json")

    t_iso = prev_ashare_trade_date()
    if not t_iso:
        _log("[abort] 无法确定上一个 A 股交易日（日历缺失）")
        return 1
    t8 = t_iso.replace("-", "")
    snap = os.path.join(ROOT, "data", "recap", f"{t8}.json")
    snap_gz = snap + ".gz"
    if not (os.path.exists(snap) or os.path.exists(snap_gz)):
        # 无快照 = 17:05 采集链失败（或长假日无 T 日行情）：美股已更新，复盘跳过
        _log(f"[skip] {t8} 无复盘快照（17:05 采集失败或非交易日链路），仅完成美股更新")
        return 0 if ok_us else 1

    steps = [
        [sys.executable, os.path.join("backend", "recap", "speculate.py"), "--date", t8],
        [sys.executable, os.path.join("backend", "recap", "check_snapshot.py"), f"data/recap/{t8}.json"],
        [sys.executable, os.path.join("backend", "derive.py")],
    ]
    rcs = [run_step(c) for c in steps]
    _log(f"==== us-close recap chain end (us={ok_us} steps={rcs}) ====")
    return 0 if all(rcs) else 1


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(main())
