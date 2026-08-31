# -*- coding: utf-8 -*-
"""网络重试 · 单一来源（2026-09-04 收敛）。

providers._retry（3 次/3s）与 fetch_global._retry（2 次/2s）是两套同构实现；
统一到这里，各文件保留同名薄转发（签名不同就传参），调用点零改动。
"""
import time


def retry(fn, tries=2, delay=2.0):
    """执行 fn，失败重试 tries 次（总尝试 tries+1 次），仍失败抛最后一个异常。"""
    last = None
    for i in range(tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 与原两处实现口径一致：由调用方决定降级
            last = e
            if i < tries:
                time.sleep(delay)
    raise last
