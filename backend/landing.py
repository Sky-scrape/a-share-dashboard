# -*- coding: utf-8 -*-
"""启动落点（server.py 与 start.py 共用的唯一口径）。

顶栏板块顺序按盯盘节奏排（竞价 → 轮动 → 复盘 → 全球 → 量化），但根路径 `/`
仍是日内轮动（老链接、书签、iframe 切换都依赖它，不宜改）。所以「一打开先看哪个板块」
由这里按时段决定：集合竞价窗口内先盯竞价，其余时间先盯轮动。
"""
import time

AUCTION_LANDING = "/auction"
DEFAULT_LANDING = "/"
# 09:10 提前一点开：计划任务 09:14 起采集，09:15 竞价开始；09:30 开盘后该看轮动了
WINDOW_START = 9 * 60 + 10
WINDOW_END = 9 * 60 + 30


def landing_path(now=None):
    """返回启动应打开的路径。now 传 time.struct_time 可测试。"""
    t = now or time.localtime()
    if t.tm_wday >= 5:                      # 周六=5 周日=6，无集合竞价
        return DEFAULT_LANDING
    minutes = t.tm_hour * 60 + t.tm_min
    if WINDOW_START <= minutes < WINDOW_END:
        return AUCTION_LANDING
    return DEFAULT_LANDING


def describe(now=None):
    """给人看的落点说明（启动横幅用）。"""
    t = now or time.localtime()
    if landing_path(t) == AUCTION_LANDING:
        return "竞价窗口（09:10–09:30 工作日）→ 先开实时竞价"
    return "常规时段 → 先开日内轮动"


if __name__ == "__main__":
    for hm, want in (((9, 9), DEFAULT_LANDING), ((9, 10), AUCTION_LANDING),
                     ((9, 29), AUCTION_LANDING), ((9, 30), DEFAULT_LANDING),
                     ((13, 0), DEFAULT_LANDING)):
        st = time.struct_time((2026, 9, 1, hm[0], hm[1], 0, 1, 1, -1))  # 周二
        got = landing_path(st)
        assert got == want, (hm, got, want)
    sat = time.struct_time((2026, 9, 5, 9, 20, 0, 5, 248, -1))          # 周六
    assert landing_path(sat) == DEFAULT_LANDING, sat
    print("landing 口径自测通过：", describe(time.struct_time((2026, 9, 1, 9, 20, 0, 1, 244, -1))))
