"""行情落盘的「数据身份证」：目录级 `_manifest.json`，记录每个标的文件的口径与元信息。

坑的来历（8/29 设计评审）：Parquet 落盘不携带复权口径——同目录下混着手放 raw CSV 时，
涨跌停判定（依赖 pre_close）与信号会整体错位且无任何告警；同一段历史在不同时间点
因分红被 qfq 改写后，两次导出结果悄悄不同。复权口径是「信任地基」，必须随数据落盘。

布局：与被导出文件同目录的 `_manifest.json`：
    {"<symbol>.parquet": {"symbol","adjust","source","exported_at","rows","first","last", ...}}
读侧：loader.load_panel 自动附带 panel.metadata["manifest"]；expect_adjust 不一致的标的
进 panel.metadata["manifest_mismatch"]，由 UI/报告醒目告警——不静默使用。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional, Tuple

from ..core.fsutil import save_json_atomic

try:
    import msvcrt      # Windows：内核字节范围锁（持有/接管步互斥）
except ImportError:
    msvcrt = None
try:
    import fcntl       # POSIX 回退：记录锁
except ImportError:
    fcntl = None

MANIFEST_NAME = "_manifest.json"

#: manifest 读-改-写窗口毫秒级；拿锁最多等 10s，超时降级为无锁直写（不挂死导出）。
_LOCK_TIMEOUT_S = 10.0
#: 锁文件超过该秒数无更新视为写进程崩溃/被杀的残留，允许接管（防死锁永久化）。
_LOCK_STALE_S = 60.0


def manifest_path(dir_path: str) -> str:
    return os.path.join(dir_path, MANIFEST_NAME)


def read_manifest(dir_path: str) -> Dict[str, dict]:
    p = manifest_path(dir_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # 坏 manifest 不阻断读取，校验环节会提示


def _lock_fd_nb(fd, unlock=False):
    """fd 当前位置起 1 字节的非阻塞排他内核锁。成功 True；被他人持有/不可用 False。

    内核保证同一文件同一时刻只有一个锁持有者，进程崩溃/句柄关闭自动释放——
    这是纯文件系统原语（O_EXCL/remove）给不了的「接管步」互斥。msvcrt 与
    fcntl 都不可用/锁不过时保守返回 False（= 永不接管，宁可不接管也不双持）。"""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if msvcrt is not None:
            msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
            return True
        if fcntl is not None:
            fcntl.lockf(fd, fcntl.LOCK_UN if unlock
                        else fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        return False
    except OSError:
        return False


def _acquire_manifest_lock(dir_path: str) -> Optional[Tuple[str, int]]:
    """目录级跨进程锁（内联同构 backend/lockutil.acquire——本包独立于 backend，
    不反向依赖）。返回 (锁路径, fd)，fd 保持打开并持有内核锁直到释放；等满
    _LOCK_TIMEOUT_S 返回 None（调用方降级无锁直写）。

    2026-09-30 修删除步双持 TOCTOU（同 backend/lockutil 2026-09-30 批）：旧实现
    「判 stale -> os.remove -> O_EXCL 重建」的 remove 不校验文件仍是采样的那把——
    两进程同判 stale 时，P1 remove 后 O_EXCL 重建的新锁会被 P2 基于陈旧采样照删
    不误，P2 再建再夺，两进程同时持锁做读-改-写互相覆盖、丢 manifest 更新
    （O_EXCL 只保护创建步，保护不了删除步）。改为锁文件全程不空缺的接管协议：
      1. O_EXCL 创建成功即上内核锁并保持到释放——持有方（含一次 update_manifest
         超 _LOCK_STALE_S 的慢写）期间任何接管者都拿不到内核锁，活锁不再被
         「超龄」误夺；
      2. stale（锁内容 at 字段超龄，数据视图、不受 Windows 目录元数据 mtime
         缓存秒级滞后影响；内容不可解析回退 mtime）时 open(O_RDWR) + 非阻塞
         内核锁仲裁接管权（零竞态窗口），持锁二次确认未被人抢先接管后原地
         改写为本进程 payload——无 remove，删除步双持消失；
      3. 崩溃残留：进程死句柄关、内核锁自动释放，接管者照常接管，不自锁死。
    内核锁不可用的平台上不持有也不接管（等待方只会等满超时降级），绝不退化
    成无仲裁的删锁互夺。"""
    import random

    p = os.path.join(dir_path, "_manifest.lock")
    deadline = time.time() + _LOCK_TIMEOUT_S
    while True:
        payload = json.dumps({"pid": os.getpid(), "at": time.time()}).encode("utf-8")
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            fd = None
        except PermissionError:
            fd = None   # Windows 微窗口：撞上他人 create 未关闭的写句柄，语义同被持有
        else:
            try:
                if not _lock_fd_nb(fd):
                    raise OSError("kernel lock unavailable")
                os.write(fd, payload)
                return p, fd
            except OSError:
                # 创建成功但内核锁不可用（msvcrt/fcntl 双缺等）：回滚创建，
                # 本进程绝不做无内核锁仲裁的持有/接管
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(p)
                except OSError:
                    pass
        # 已被持有：采样锁内容判 stale（读到接管者半写瞬间会解析失败，稍候
        # 重读一次；持续不可解析才回退 mtime 判定 + mtime 锚定接管）
        old_payload = old_at = mtime_anchor = None
        for attempt in (0, 1):
            try:
                with open(p, "rb") as f:
                    old_payload = f.read().decode("utf-8", "replace")
            except OSError:
                old_payload = None
                break   # 锁文件刚好被释放/被移走：回头再走一轮 O_EXCL
            try:
                old_at = float(json.loads(old_payload).get("at"))
            except (ValueError, TypeError, AttributeError):
                old_at = None
            if old_at is not None or attempt:
                break
            time.sleep(0.005)
        if old_payload is None:
            if time.time() >= deadline:
                return None
            continue
        if old_at is not None:
            stale = (time.time() - old_at) > _LOCK_STALE_S
        else:
            try:
                mtime_anchor = os.path.getmtime(p)
                stale = (time.time() - mtime_anchor) > _LOCK_STALE_S
            except OSError:
                if time.time() >= deadline:
                    return None
                continue   # 锁刚好被释放：回头再走一轮 O_EXCL
        if not stale:
            if time.time() >= deadline:
                return None
            time.sleep(0.02 + random.uniform(0.0, 0.03))
            continue
        # stale：内核锁仲裁下的原地接管（无 remove，锁文件全程不空缺）
        try:
            tfd = os.open(p, os.O_RDWR)
        except OSError:
            if time.time() >= deadline:
                return None
            continue   # 锁刚好被释放：回头再走一轮 O_EXCL
        try:
            if not _lock_fd_nb(tfd):
                raise OSError("contended")   # 活持有者握锁/他人接管中：按被持有
            if old_at is not None:
                if os.read(tfd, 65536).decode("utf-8", "replace") != old_payload:
                    raise OSError("takeover race")   # 已被他人接管改写：按被持有
            elif os.fstat(tfd).st_mtime != mtime_anchor:
                raise OSError("takeover race")
            os.lseek(tfd, 0, os.SEEK_SET)
            os.write(tfd, payload)
            os.ftruncate(tfd, len(payload))
            return p, tfd
        except OSError:
            try:
                os.close(tfd)
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.02 + random.uniform(0.0, 0.03))
            continue


def _release_manifest_lock(lock: Optional[Tuple[str, int]]) -> None:
    """释放本进程的锁：解内核锁关 fd，内容 pid 校验通过才 remove——接管发生时
    锁内容已是接管者的 pid，不误删（同 lockutil.release 的 2026-09-14 口径）；
    读不出/已被移走一律不删，残留由接管协议兜底。
    remove 遇 PermissionError 短重试（2026-09-30 4 进程压测发现）：Windows 上
    等待方的采样读句柄（CPython open 不带 FILE_SHARE_DELETE）开着期间 remove
    会被 sharing violation 拒绝，μs 级 open-read-close 窗口撞上即锁文件残留——
    残留锁内容 at 新鲜，等待方只能干等满 _LOCK_TIMEOUT_S 降级直写丢更新。"""
    if not lock:
        return
    p, fd = lock
    _lock_fd_nb(fd, unlock=True)   # 失败不阻断：close 会隐式释放
    try:
        os.close(fd)
    except OSError:
        pass
    for _ in range(40):
        try:
            with open(p, encoding="utf-8") as f:
                pid = json.loads(f.read()).get("pid")
            if pid != os.getpid():
                return   # 锁已被接管：不能删接管者的锁
            os.remove(p)
            return
        except PermissionError:
            time.sleep(0.005)   # 采样读句柄的 μs 级窗口，短重试穿过
        except (OSError, ValueError):
            return   # 已被移走/不可解析：不误删，残留走接管协议兜底


def update_manifest(dir_path: str, entries: Dict[str, dict]) -> None:
    """增量合并写入（读-改-写）。

    2026-09-30 跨进程锁：server 同步回测线程与后台任务子进程会并发导出同一
    共享目录，原子写只防撕裂不防丢更新——两进程同时读-改-写时后写者整个覆盖
    前写者，丢掉的 manifest 条目使 _cache_covers 判未覆盖、expect_adjust 口径
    校验对该文件静默空转。锁内完成整个读-改-写；极端争用拿不到锁时降级为
    旧版无锁直写，不阻断导出。"""
    if not entries:
        return
    lock = _acquire_manifest_lock(dir_path)
    try:
        p = manifest_path(dir_path)
        merged = read_manifest(dir_path)
        for fname, meta in entries.items():
            merged[fname] = meta
        try:
            save_json_atomic(p, merged, indent=1, sort_keys=True)
        except OSError:
            pass  # 只读目录等极端情况：数据本身已落盘，不因元信息失败而炸导出
    finally:
        _release_manifest_lock(lock)


def entry_for(frame, *, adjust: str, source: str, **extra) -> dict:
    """从导出的 DataFrame 提取标准元信息。"""
    e = {
        "adjust": adjust,
        "source": source,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": int(len(frame)),
        "first": str(frame.index.min().date()),
        "last": str(frame.index.max().date()),
    }
    e.update({k: v for k, v in extra.items() if v not in (None, "", 0, {})})
    return e


def check_adjust(manifest: Dict[str, dict], expect_adjust: Optional[str]) -> Dict[str, str]:
    """返回个股口径不一致清单 {file: "实际口径"}。

    只校验同词表（qfq/hfq/raw）的个股文件：ETF 的 dividend_reinvested、指数的 none
    是各自链路的固有口径，不是错配，不参与股票口径比较（否则混装面板全是误报）。
    无/未知口径的文件不判（兼容手工放的非 hithink 数据）。
    """
    if not expect_adjust or not manifest:
        return {}
    stock_vocab = {"qfq", "hfq", "raw"}
    want = str(expect_adjust).lower()
    if want not in stock_vocab:
        return {}
    out: Dict[str, str] = {}
    for fname, meta in manifest.items():
        got = str((meta or {}).get("adjust", "")).lower()
        if got in stock_vocab and got != want:
            out[fname] = got
    return out
