# -*- coding: utf-8 -*-
"""跨进程文件锁（Windows 友好）：O_EXCL 创建锁文件，内容 pid@epoch。

- acquire(): 拿到返回锁路径；已被持有且未过期返回 None；过期锁（默认 40 分钟）自动接管。
- release(): 释放（只删自己拿到的）。
抓取脚本（计划任务/手动）与 server.py 的 /api/fetch 共用同一把锁，杜绝并发重入。
"""
import json
import os
import time

try:
    import msvcrt      # Windows：内核字节范围锁
except ImportError:
    msvcrt = None
try:
    import fcntl       # POSIX 回退：记录锁
except ImportError:
    fcntl = None

DEFAULT_STALE_MIN = 40


def _lock_fd_nb(fd, unlock=False):
    """fd 当前位置起 1 字节的非阻塞排他锁。成功 True；被他人持有/无内核锁 False。

    内核保证同一文件同一时刻只有一个锁持有者，进程退出/句柄关闭自动释放——
    这是纯文件系统原语（O_EXCL/remove/rename 全是路径级操作）给不了的
    「接管步」互斥。msvcrt.locking 与 fcntl.lockf 都锁不过则保守返回 False
    （= 永不接管，宁可不接管也不双持）。
    """
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


def _takeover_stale(lock_path, old_payload, mtime_anchor, payload):
    """过期锁的原地接管（内核字节锁仲裁 + 内容世代锚定）。返回 True=接管成功；
    False=放弃接管（他人接管中/锁已被他人接管改写/锁刚好被释放）。

    2026-09-30 修删除步 TOCTOU：旧实现「判过期 -> os.remove」的 remove 不校验
    文件仍是锚定的那把——两进程同判过期时，P1 remove 后 O_EXCL 重建的新锁
    会被 P2 基于陈旧采样照删不误，P2 再建再夺，两进程同时持锁（O_EXCL 只
    保护创建步，保护不了删除步）。

    协议（本机 8 进程压测逐版淘汰出的终版）：
      1. open(LP, O_RDWR)——CPython 的 os.open 共享模式不含 FILE_SHARE_DELETE，
         句柄开着期间任何第三方都 remove/rename 不了 LP；
      2. 非阻塞排他内核锁——同一时刻只允许一个接管者（零竞态窗口，进程崩溃
         自动释放，与 stale_min 的「写进程死亡」语义天然一致）；
      3. 持锁二次确认：锁内重读内容必须与采样一致（old_payload is None 时
         回退 fstat mtime）——内核锁保证持锁期间无人能改写，不一致 ⟹
         「采样→拿锁」间隙里锁已被他人接管成活锁，放弃（按被持有处理）；
      4. 原地改写内容为本进程 payload + 截断——锁文件全程存在、不空缺。

    锚定与过期判定统一用「文件数据」（read 内容）而非 mtime：Windows 目录
    元数据缓存有秒级滞后（实测），改写后的新锁经路径 stat 可能仍读到旧
    mtime，令后来者误判过期、连 fstat 二次确认也被同一缓存欺骗（压测实测
    1/300 轮双持）；锁内容里的 at 字段与「采样内容比对」同走数据视图，无
    此滞后。仅内容不可解析（手建/残缺锁）时回退 mtime 锚定——接受该罕见
    输入下的残余窗口以保留「可接管」语义。
    """
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False   # 锁刚好被释放：调用方回头走 O_EXCL（拿不到就认）
    try:
        if not _lock_fd_nb(fd):
            return False   # 他人正在接管（内核锁互斥）：按被持有处理
        try:
            if old_payload is not None:
                if os.read(fd, 65536).decode("utf-8", "replace") != old_payload:
                    return False   # 拿锁前锁已被他人接管改写：按被持有处理
            elif os.fstat(fd).st_mtime != mtime_anchor:
                return False   # 同上（内容不可解析时的 mtime 锚定回退）
            data = payload.encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, len(data))
        except OSError:
            return False   # 写失败保持原样：仍是过期锁，语义不变
        # close 隐式释放内核锁并保证改写数据对新来者可见；先返回后 close 会让
        # 下一接管者拿到锁却读到旧数据（未验证此路径，保持 close-先于-return）。
        os.close(fd)
        fd = None
        return True
    finally:
        if fd is not None:
            _lock_fd_nb(fd, unlock=True)
            os.close(fd)


def acquire(lock_path, stale_min=DEFAULT_STALE_MIN):
    """拿锁：O_EXCL 原子创建；被持有且未过期返回 None；过期锁原子接管。

    2026-09-10 修 TOCTOU（创建步）：接管也必须走原子创建，两进程同时重建时
    只有一个 os.open 成功。
    2026-09-30 修 TOCTOU（删除步）：接管不再「先删后建」（该删除步存在双持
    TOCTOU，且纯文件系统替代协议均有残余窗口），改走 _takeover_stale——
    内核字节锁仲裁 + 内容世代锚定 + 原地改写，锁文件全程不空缺。
    过期判定读锁内容里的 at 字段（数据视图，不受 Windows 目录元数据缓存
    滞后影响）；内容不可解析时回退 mtime 判定 + mtime 锚定接管。
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
        except PermissionError:
            # Windows 微窗口：撞上他人 create 尚未关闭的写句柄时抛的是
            # PermissionError 而非 FileExistsError——语义同为「已被占用」，
            # 按被持有处理，不让 acquire 崩掉调用方（2026-09-30 审查副发现）。
            pass
        # 采样：读内容 + 解析 at（数据视图，与接管锚定同源——Windows 目录
        # 元数据缓存有秒级滞后，路径 stat 的 mtime 不可靠）。读到接管者半写
        # 瞬间（μs 级）会解析失败，稍候重读一次；持续不可解析（手建/残缺锁）
        # 才回退 mtime 判定与锚定。
        old_payload = old_at = mtime_anchor = None
        for attempt in (0, 1):
            try:
                with open(lock_path, encoding="utf-8") as f:
                    old_payload = f.read()
            except OSError:
                old_payload = None
                break   # 锁文件刚好被释放：回头再走一轮 O_EXCL
            try:
                old_at = float(json.loads(
                    old_payload.split(" (stolen")[0]).get("at"))
            except (ValueError, TypeError, AttributeError):
                old_at = None
            if old_at is not None or attempt:
                break
            time.sleep(0.005)
        if old_payload is None:
            continue
        if old_at is not None:
            if (time.time() - old_at) / 60 <= stale_min:
                return None  # 仍被持有
        else:
            # 持续不可解析（手建/残缺锁）：mtime 判定 + mtime 锚定接管
            try:
                mtime_anchor = os.path.getmtime(lock_path)
                age_min = (time.time() - mtime_anchor) / 60
            except OSError:
                continue
            if age_min <= stale_min:
                return None  # 仍被持有
        # 过期锁（写进程崩溃/被杀后残留）：内核锁仲裁下的原地接管
        if _takeover_stale(lock_path,
                           old_payload if old_at is not None else None,
                           mtime_anchor, payload):
            return lock_path
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
    except Exception:  # noqa: BLE001
        return None


def release(lock_path, force=False):
    """释放锁。默认只删内容 PID 等于本进程的锁：40 分钟过期被接管后，原持有者
    的 finally 不再误删接管者的锁（2026-09-14 修；此前 release 是无差别 remove）。
    force=True 供 server 轮动自愈「摘死锁」这类刻意的跨进程接管场景。锁内容
    读不出来（残缺/手建）时按他人锁处理——宁可不删（有过期接管兜底）也不误删。
    """
    try:
        if not (lock_path and os.path.isfile(lock_path)):
            return
        if not force:
            try:
                with open(lock_path, encoding="utf-8") as f:
                    pid = json.loads(f.read().split(" (stolen")[0]).get("pid")
            except Exception:  # noqa: BLE001
                return
            if pid != os.getpid():
                return
        os.remove(lock_path)
    except Exception:  # noqa: BLE001
        pass
