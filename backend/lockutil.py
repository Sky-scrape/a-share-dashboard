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
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "at": time.time()})
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return lock_path
    except FileExistsError:
        try:
            age_min = (time.time() - os.path.getmtime(lock_path)) / 60
            held = ""
            try:
                with open(lock_path, encoding="utf-8") as f:
                    held = f.read()[:120]
            except Exception:
                pass
            if age_min > stale_min:
                # 过期锁：接管（写进程崩溃/被杀后残留）
                with open(lock_path, "w") as f:
                    f.write(payload + " (stolen after %.0f min)" % age_min)
                return lock_path
            return None  # 仍被持有
        except Exception:
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
