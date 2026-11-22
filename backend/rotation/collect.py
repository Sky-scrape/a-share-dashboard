# -*- coding: utf-8 -*-
"""【LEGACY · 东财口径（2026-09-01 退役）】盘中定时采集器（旧）。

新采集器 ths_collect.py 自带盘中外循环（等待开盘/午间休市/收盘定格），
本包装脚本不再接入计划任务；保留仅供参考。

原设计：交易时段内每隔 N 分钟拉取一次当日板块分时，
覆盖写入 LEGACY 归档 daily_legacy_eastmoney/<今天>.json（数据 = 截至当前时刻）。
调用 fetch_day.py 时固定传 --legacy；现行 data/rotation/daily/ 归 ths_collect.py。

用法:
    python collect.py                  # 每 5 分钟采集一次
    python collect.py --interval 10    # 每 10 分钟一次

说明:
    - 仅在 9:30-11:30 / 13:00-15:00 交易时段采集，非交易时段自动退出
    - 收盘后数据已完整，直接运行 python fetch_day.py --legacy 即可
    - 长期监控建议用系统计划任务启动，或配合 Proma 定时任务
"""
import datetime
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_DAY = os.path.join(BASE_DIR, "fetch_day.py")
# 固定实参（全部来自本文件常量，无外部输入）：显式 --legacy 走 LEGACY 归档口径
FETCH_DAY_ARGV = [sys.executable, FETCH_DAY, "--legacy"]


def in_trading_time(now):
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def _fetch_once():
    """跑一次 fetch_day.py：实参全是本文件常量（无外部输入），参数向量直传、不经过 shell。

    固定带 --legacy：LEGACY 东财口径只写归档目录 daily_legacy_eastmoney/。
    """
    return os.spawnv(os.P_WAIT, sys.executable, FETCH_DAY_ARGV)


def main():
    interval = 5
    if "--interval" in sys.argv:
        i = sys.argv.index("--interval")
        interval = int(sys.argv[i + 1])
    print(f"盘中采集器启动：每 {interval} 分钟采集一次（交易时段内运行）")
    while True:
        now = datetime.datetime.now()
        if not in_trading_time(now):
            print(f"[{now:%H:%M}] 当前非交易时段，采集结束。收盘后请运行: python fetch_day.py")
            break
        print(f"[{now:%H:%M}] 开始采集（截至当前时刻）...")
        _fetch_once()
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
