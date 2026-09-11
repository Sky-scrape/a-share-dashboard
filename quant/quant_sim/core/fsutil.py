# -*- coding: utf-8 -*-
"""原子写盘 helper（quant_sim 内部版 · 2026-09-11 收敛）。

与 backend/fsutil.py 同一语义：同目录临时文件 + os.replace（Windows 下目标被
短暂打开时 PermissionError，50ms × 3 短重试）+ 异常清理临时文件。quant_sim 是
独立包、不反向依赖 backend，故保留一份同构实现；两边落盘产物逐字节兼容
（JSON 一律 ensure_ascii=False + utf-8）。

临时文件用 tempfile.mkstemp（O_EXCL 安全创建、同目录保证同盘，跨盘 replace 会
退化为拷贝）；文件名形如 .<name>.tmp<rand>，不会被各读方的 glob/清理逻辑误认。

背景：manifest 写半截会让 read_manifest 返回 {} → 缓存覆盖判据静默失效、
check_adjust 空转；策略库/台账半截则是用户数据损坏。落盘一律走这里。
"""
import json
import os
import tempfile
import time
from typing import Any, Callable, IO, Optional

_REPLACE_RETRIES = 3
_REPLACE_RETRY_WAIT = 0.05   # 50ms：读方持句柄的窗口极短，短重试足矣


def _safe_dst(dst: str) -> str:
    """入口校验 + 规范化：abspath/normpath 消除 `./` 与内嵌 `..` 后，再查一次
    不残留 `..` 路径段（路径穿越防线；本包调用方均为程序内拼出的固定目录 +
    固定文件名，正常不会触发）。"""
    dst = os.path.normpath(os.path.abspath(str(dst)))
    if ".." in dst.replace("\\", "/").split("/"):
        raise ValueError(f"写目标路径不允许包含 '..': {dst}")
    return dst


def _replace_retry(tmp: str, dst: str) -> None:
    for i in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, dst)
            return
        except OSError:
            if i == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_WAIT)


def _atomic_write(dst: str, write_fn: Callable[[IO], None],
                  encoding: str = "utf-8", newline: Optional[str] = None) -> str:
    """公共骨架：规范化校验 -> mkstemp 临时文件写 -> replace -> 异常清理临时文件。"""
    dst = _safe_dst(dst)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst),
                               prefix=f".{os.path.basename(dst)}.tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            write_fn(f)
        _replace_retry(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def save_text_atomic(path: str, text: str, encoding: str = "utf-8",
                     newline: Optional[str] = None) -> str:
    """文本原子写。newline 语义同 open()：缺省 None 与 Path.write_text 一致
    （\\n 按平台翻译）；传 "\\n" 可强制 LF 落盘。"""
    return _atomic_write(path, lambda f: f.write(text), encoding=encoding,
                         newline=newline)


def save_json_atomic(path: str, obj: Any, indent: Optional[int] = None,
                     sort_keys: bool = False, default=None,
                     ensure_ascii: bool = False, encoding: str = "utf-8") -> str:
    """JSON 原子写（统一 ensure_ascii=False + utf-8；indent/sort_keys/default 可选）。"""
    text = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent,
                      sort_keys=sort_keys, default=default)
    return save_text_atomic(path, text, encoding=encoding)
