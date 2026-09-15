# -*- coding: utf-8 -*-
"""计划任务脚本的推送出口（供 .bat 在采集失败后调用，2026-09-15）。

用法（fetch_task.bat 尾部，RC != 0 时）：
    python backend\\notify_once.py recap-failed

行为：
    recap-failed   读 .status/recap.json，若有失败模块则推送失败明细
                   （17:05 采集链失败的主动告警；成功/部分成功不推——
                   单模块降级由 health 监控线程按 key 节流兜底）。

设计边界：尽力而为——读不到状态文件/未配置渠道/推送失败都静默退出 0，
绝不反过来让计划任务因告警失败而报错。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # backend/
sys.path.insert(0, HERE)                            # backend/recap/

PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
RECAP_STATUS = os.path.join(PROJECT_ROOT, ".status", "recap.json")

import notify  # noqa: E402


def recap_failed():
    try:
        with open(RECAP_STATUS, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001 - 状态文件读不到就没内容可推
        return
    failed = st.get("failed") or []
    if not failed:
        return
    mods = (st.get("modules") or {})
    bad = [k for k, v in mods.items() if v not in ("ok", None)]
    notify.send(
        title="复盘采集失败",
        text=(f"17:05 复盘采集有失败模块（{st.get('date') or '?'}）：\n"
              + "\n".join(f"- {m}" for m in failed[:15])
              + (f"\n\n状态明细异常模块：{', '.join(bad[:15])}" if bad else "")
              + "\n\n白天可打开看板在新鲜度胶囊里手动重抓。"),
        key="fetch:recap-failed", min_interval_hours=12)


MODES = {"recap-failed": recap_failed}


def main():
    ap = argparse.ArgumentParser(description="计划任务推送出口（尽力而为，退出码恒为 0）")
    ap.add_argument("mode", choices=sorted(MODES))
    args = ap.parse_args()
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    try:
        MODES[args.mode]()
    except Exception:  # noqa: BLE001 - 推送出口绝不反向打断任务链
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
