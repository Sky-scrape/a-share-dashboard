# -*- coding: utf-8 -*-
"""跨进程文件锁（Windows 友好）：O_EXCL 创建锁文件，内容 pid@epoch。

- acquire(): 拿到返回锁路径；已被持有且未过期返回 None；过期锁（默认 40 分钟）自动接管。
- release(): 释放（只删自己拿到的）。
抓取脚本（计划任务/手动）与 server.py 的 /api/fetch 共用同一把锁，杜绝并发重入。
"""
import json
import os
import time

DEFAULT_STALE_MIN = 40


def acquire(lock_path, stale_min=DEFAULT_STALE_MIN):
    """拿锁：O_EXCL 原子创建；被持有且未过期返回 None；过期锁先删后重夺。

    2026-09-10 修 TOCTOU：旧实现「查 mtime 过期 -> write_text 覆盖」两步之间
    另一进程同样判定过期并覆盖，两进程同时持有锁。现在接管也必须走 O_EXCL
    （删除后重建仍用原子创建），两进程同时接管时只有一个 os.open 成功。
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    for _ in range(3):
        payload = json.dumps({"pid": os.getpid(), "at": time.time()})
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            return lock_path
        except FileExistsError:
            pass
        try:
            age_min = (time.time() - os.path.getmtime(lock_path)) / 60
        except OSError:
            continue   # 锁文件刚好被释放：回头再走一轮 O_EXCL
        if age_min <= stale_min:
            return None  # 仍被持有
        # 过期锁（写进程崩溃/被杀后残留）：先删，下一轮 O_EXCL 原子重夺
        try:
            os.remove(lock_path)
        except OSError:
            return None  # 删除失败=他人正在接管/释放，按被持有处理
    return None


def held_info(lock_path):
    """锁被持有时返回 {pid, at}，否则 None。"""
    if not os.path.isfile(lock_path):
        return None
    try:
        age_min = (time.time() - os.path.getmtime(lock_path)) / 60
        if age_min > DEFAULT_STALE_MIN:
            return None
        with open(lock_path, encoding="utf-8") as f:
            d = json.loads(f.read().split(" (stolen")[0])
        d["age_min"] = round(age_min, 1)
        return d
    except Exception:
        return None


def release(lock_path):
    try:
        if lock_path and os.path.isfile(lock_path):
            os.remove(lock_path)
    except Exception:
        pass
