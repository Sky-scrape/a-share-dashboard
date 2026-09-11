# -*- coding: utf-8 -*-
"""快照读写单一入口：兼容明文 .json 与归档 .json.gz。

谁都不该再直接 glob/open 快照文件，统一走这里（server / derive / check / archive / fetch）。

- load：文件缺失返回 None（原语义）；损坏/IO 异常不再静默吞掉——记日志（路径 +
  异常类型）后返回 None，损坏与缺失从此可辨；
- load 带进程内 (路径, mtime, size) 键的小型 FIFO 缓存（上限 8 份）：一天内同一
  份快照被 validate_prev_pool / providers.speculation / server 反复解析，此处
  收口后只读一次盘。返回对象为缓存共享，调用方不得原地修改（retrofill 这类
  要改写快照的调用方自行 deepcopy）；文件被原子写覆盖后 mtime 变化自动失效；
- save / 归档写统一走 backend/fsutil.py 原子写（写盘纪律：临时文件 + replace）。
"""
import gzip
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_BACKEND_ROOT = os.path.dirname(_HERE)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import fsutil  # noqa: E402
from logutil import get_logger  # noqa: E402  统一 logging（backend/logutil.py）
from modules import MODULES, TITLES, ALLOW_EMPTY  # noqa: F401  (re-export 便捷)

LOG = get_logger("ak.snapio")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECAP_DATA = os.path.join(ROOT, "data", "recap")
SNAP_RE = re.compile(r"\d{8}")

# 进程内已解析快照缓存：fp -> (mtime, size, doc)。FIFO 上限 8 份，防长驻进程无界膨胀
_LOAD_CACHE = {}
_LOAD_CACHE_MAX = 8


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
    """读一份快照，自动处理 gzip。

    缺失（文件不存在）返回 None；损坏（JSONDecodeError/BadGzipFile 等）与 IO
    异常记 warning 日志（含路径与异常类型）后返回 None——保持调用方「None 即
    无数据」的降级语义，但日志里可辨「缺」与「坏」。
    """
    fp = snap_path(recap_dir or RECAP_DATA, date8)
    if not os.path.isfile(fp):
        return None
    try:
        stat = (os.path.getmtime(fp), os.path.getsize(fp))
    except OSError:
        return None
    ent = _LOAD_CACHE.get(fp)
    if ent and ent[0] == stat[0] and ent[1] == stat[1]:
        return ent[2]
    try:
        if fp.endswith(".gz"):
            with gzip.open(fp, "rt", encoding="utf-8") as f:
                doc = json.load(f)
        else:
            with open(fp, encoding="utf-8") as f:
                doc = json.load(f)
    except FileNotFoundError:
        return None   # 读取瞬间被删（归档/清理竞态）：按缺失处理
    except Exception as e:  # noqa: BLE001 - 损坏/IO 异常：日志留痕后按无数据处理
        LOG.warning("快照读取失败 %s: %s: %s", fp, type(e).__name__, str(e)[:200])
        return None
    _LOAD_CACHE[fp] = (stat[0], stat[1], doc)
    if len(_LOAD_CACHE) > _LOAD_CACHE_MAX:
        for k in list(_LOAD_CACHE)[:len(_LOAD_CACHE) - _LOAD_CACHE_MAX]:
            _LOAD_CACHE.pop(k, None)
    return doc


def save(date8, obj, recap_dir=None):
    """原子写明文快照（backend/fsutil.save_json_atomic，临时文件 + replace）。"""
    d = recap_dir or RECAP_DATA
    return fsutil.save_json_atomic(os.path.join(d, date8 + ".json"), obj, indent=2)
