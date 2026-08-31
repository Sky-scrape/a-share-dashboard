# -*- coding: utf-8 -*-
"""快照读写单一入口：兼容明文 .json 与归档 .json.gz。

谁都不该再直接 glob/open 快照文件，统一走这里（server / derive / check / archive / fetch）。
"""
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules import MODULES, TITLES, ALLOW_EMPTY  # noqa: F401  (re-export 便捷)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECAP_DATA = os.path.join(ROOT, "data", "recap")
SNAP_RE = re.compile(r"\d{8}")


def snap_path(recap_dir, date8):
    """返回存在的快照路径（.json 优先，其次 .json.gz），都不存在返回 .json 名义路径。"""
    p = os.path.join(recap_dir, date8 + ".json")
    if os.path.isfile(p):
        return p
    return os.path.join(recap_dir, date8 + ".json.gz")


def list_dates(recap_dir=None):
    """全部快照日期（8位数字，倒序）。"""
    d = recap_dir or RECAP_DATA
    if not os.path.isdir(d):
        return []
    out = set()
    for f in os.listdir(d):
        if f.endswith(".json") and SNAP_RE.fullmatch(f[:-5]):
            out.add(f[:-5])
        elif f.endswith(".json.gz") and SNAP_RE.fullmatch(f[:-8]):
            out.add(f[:-8])
    return sorted(out, reverse=True)


def load(date8, recap_dir=None):
    """读一份快照，自动处理 gzip；失败返回 None。"""
    fp = snap_path(recap_dir or RECAP_DATA, date8)
    try:
        if fp.endswith(".gz"):
            with gzip.open(fp, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(date8, obj, recap_dir=None):
    """原子写明文快照。"""
    d = recap_dir or RECAP_DATA
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, date8 + ".json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)
    return out
