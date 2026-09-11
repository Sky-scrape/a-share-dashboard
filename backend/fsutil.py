# -*- coding: utf-8 -*-
"""原子写盘 helper · 单一来源（2026-09-10 收敛）。

此前 20+ 处 Path(...).write_text / gzip 直写 JSON：进程中途被杀（计划任务超时、
断电、Ctrl+C）会留下半截文件，读方（server 每次刷新读 live.json、derive 读
快照）拿到的就是残缺数据。统一为「同目录临时文件 + os.replace」：

- replace 在同一文件系统上原子生效，读者要么看到旧文件要么看到新文件，
  永远不会看到半截；
- 临时文件用 tempfile.mkstemp（O_EXCL 安全创建），放同目录保证同盘（跨盘
  replace 退化为拷贝），名字形如 .<name>.tmp<rand> 隐藏文件——不会被读方的
  glob / 清理逻辑误认；
- os.replace 在 Windows 下目标正被读进程打开的瞬间会失败（PermissionError），
  所有读方都是 with open 短暂读、窗口极小——失败短重试（50ms × 3）兜底；
- 异常时清理临时文件，不留垃圾。

写盘纪律（项目约定）：JSON 一律 ensure_ascii=False + utf-8；调用方只给
Path/str 路径与内容，不再各自拼 tmp/replace。
"""
import gzip
import json
import os
import tempfile
import time

_REPLACE_RETRIES = 3
_REPLACE_RETRY_WAIT = 0.05   # 50ms：读方持句柄的窗口极短，短重试足矣


def _safe_dst(dst) -> str:
    """入口校验 + 规范化：abspath/normpath 消除 `./` 与内嵌 `..` 后，再查一次
    不残留 `..` 路径段（路径穿越防线；调用方均为程序内拼出的固定目录 +
    固定文件名，正常不会触发）。"""
    dst = os.path.normpath(os.path.abspath(str(dst)))
    if ".." in dst.replace("\\", "/").split("/"):
        raise ValueError(f"写目标路径不允许包含 '..': {dst}")
    return dst


def _replace_retry(tmp, dst):
    """os.replace 带短重试：Windows 下目标被读进程短暂打开时 PermissionError 兜底。"""
    for i in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, dst)
            return
        except OSError:
            if i == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_WAIT)


def _atomic_write_text(dst, text, encoding="utf-8", newline=None):
    dst = _safe_dst(dst)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst),
                               prefix=f".{os.path.basename(dst)}.tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
        _replace_retry(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def _atomic_write_bytes(dst, data):
    dst = _safe_dst(dst)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst),
                               prefix=f".{os.path.basename(dst)}.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _replace_retry(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def save_text_atomic(path, text, encoding="utf-8", newline=None):
    """文本原子写（含 CSV/面板等；utf-8-sig 等特殊编码走 encoding 参数）。

    newline 语义同 open()：缺省 None 与 Path.write_text 一致（\\n 按平台翻译）；
    传 "\\n" 可强制 LF 落盘（如 markdown 笔记）。"""
    return _atomic_write_text(path, text, encoding=encoding, newline=newline)


def save_json_atomic(path, obj, indent=None, separators=None,
                     ensure_ascii=False, encoding="utf-8",
                     default=None, sort_keys=False):
    """JSON 原子写（统一 ensure_ascii=False + utf-8；indent/交互式压缩比可选；
    default/sort_keys 透传 json.dumps——含非原生类型（如 numpy 标量）的结果落盘用）。"""
    text = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent,
                      separators=separators, default=default, sort_keys=sort_keys)
    return save_text_atomic(path, text, encoding=encoding)


def save_bytes_atomic(path, data):
    """二进制原子写（归档 .gz 等已压缩 payload 用，避免二次压缩）。"""
    return _atomic_write_bytes(path, data)


def save_gzip_json_atomic(path, obj, compresslevel=9, ensure_ascii=False):
    """gzip 压缩的 JSON 原子写（快照归档 / concept_map 时点存档共用）。"""
    blob = gzip.compress(json.dumps(obj, ensure_ascii=ensure_ascii).encode("utf-8"),
                         compresslevel=compresslevel)
    return save_bytes_atomic(path, blob)
