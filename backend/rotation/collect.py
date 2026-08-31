# -*- coding: utf-8 -*-
"""【LEGACY · 东财口径（2026-09-01 退役）】盘中定时采集器（旧）。

新采集器 ths_collect.py 自带盘中外循环（等待开盘/午间休市/收盘定格），
本包装脚本不再接入计划任务；保留仅供参考。

原设计：交易时段内每隔 N 分钟拉取一次当日板块分时，
覆盖写入 data/daily/<今天>.json（数据 = 截至当前时刻）。

用法:
    python collect.py                  # 每 5 分钟采集一次
    python collect.py --interval 10    # 每 10 分钟一次

说明:
    - 仅在 9:30-11:30 / 13:00-15:00 交易时段采集，非交易时段自动退出
    - 收盘后数据已完整，直接运行 python fetch_day.py 即可
    - 长期监控建议用系统计划任务启动，或配合 Proma 定时任务
"""
import datetime
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_DAY = os.path.join(BASE_DIR, "fetch_day.py")


def in_trading_time(now):
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


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
        subprocess.call([sys.executable, FETCH_DAY])
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
