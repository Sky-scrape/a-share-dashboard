# -*- coding: utf-8 -*-
"""竞价数据每日归档（data/auction/archive/auc-YYYYMMDD.json.gz）。

为什么需要：data/auction/ 下 live/final/series/rounds_meta 等是「当日覆盖式」产物，
次日 09:14 采集即整份覆盖。竞价维度（开盘缺口、量比昨、未匹配量、逐轮演变）此前
没有任何历史留存，任何以竞价为输入的假设都无法回测——只能先归档、再研究。
（2026-09-24 复盘：C7 规则层优化空间已穷尽，新维度里竞价最可及，却因无历史而卡住。）

归档范围 = 当日竞价产物中承载研究价值的部分：
    final / series / rounds_meta / benchmark / sector / watchmap
不归档 live.json：它是 09:24 最后一轮的中间态，信息已被 series 覆盖，留它只会
引入「live 与 series 不同源」的歧义。
不归档 industry_map.json / ht_catalog_industry.json：前者是每 3 天重建的静态映射、
后者是接口目录快照，均不随交易日变化，无逐日留存价值。

同一交易日重复调用幂等覆盖（fsutil 原子写）。压缩后约 61KB/日 ≈ 15MB/年。
"""
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import auc_config as C  # noqa: E402  竞价配置/落盘单一来源
import fsutil  # noqa: E402  原子写盘单一来源（backend/fsutil.py）
from logutil import get_logger  # noqa: E402

LOG = get_logger("ak.auction")

ARCHIVE_DIR = os.path.join(C.DATA_DIR, "archive")
ARCHIVE_VERSION = 1
# 归档文件名 → 产物文件名。顺序即归档 JSON 内的键序，便于人工比对。
FILES = ("final", "series", "rounds_meta", "benchmark", "sector", "watchmap")


def archive_path(date8):
    """某交易日的归档文件路径。"""
    return os.path.join(ARCHIVE_DIR, f"auc-{date8}.json.gz")


def build_archive(date8, load=None):
    """组装归档 payload；缺失的产物按缺省跳过（只收当日真实落盘的部分）。

    load 可注入（测试用），缺省走 auc_config.load_json。
    """
    load = load or C.load_json
    files = {}
    for name in FILES:
        obj = load(name + ".json")
        if obj is not None:
            files[name] = obj
    return {"v": ARCHIVE_VERSION, "date": str(date8),
            "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": files}


def archive(date8=None, quiet=False):
    """归档当日竞价产物。返回 {ok, path, bytes, files, error}；失败只告警不抛。

    调用点：auc_collector 的 run_once（手动补抓）与 _timeline（09:25 定盘终态），
    都放在 fetch_sector 之后——sector.json 是最后落盘的一份。
    """
    date8 = str(date8 or datetime.now().strftime("%Y%m%d"))
    res = {"ok": False, "date": date8, "path": archive_path(date8),
           "bytes": None, "files": [], "error": None}
    try:
        payload = build_archive(date8)
        if not payload["files"]:
            res["error"] = "当日无竞价产物可归档"
            LOG.warning(f"[archive] {date8}: {res['error']}")
            return res
        # fsutil 原子写在目标目录内建临时文件（mkstemp），不自动建目录
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        fsutil.save_gzip_json_atomic(res["path"], payload)
        res["bytes"] = os.path.getsize(res["path"])
        res["files"] = sorted(payload["files"])
        res["ok"] = True
        if not quiet:
            LOG.info(f"[archive] {date8}: {res['bytes']//1024}KB "
                     f"({'+'.join(res['files'])}) -> {os.path.relpath(res['path'])}")
    except Exception as e:  # noqa: BLE001 - 归档失败绝不拖垮采集主流程
        res["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.warning(f"[archive] {date8}: 归档失败 {res['error']}")
    finally:
        # 全部出口都落状态（含「无产物」早退）——早退绕过尾部正是 F1 的教训
        _record_status(date8, res)
    return res


def _record_status(date8, res):
    """把归档结果并入 .status/auction.json（读改写；失败忽略）。

    归档若长期静默失败就等于没有归档——本项目的 SSRF 守卫误杀正是「静默降级」
    的教训，所以让 /api/health 与体检面板看得见。
    """
    try:
        path = C.STATUS_JSON
        st = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        if not isinstance(st, dict):
            st = {}
        st["archive"] = {"date": date8, "ok": res["ok"],
                         "bytes": res["bytes"], "files": res["files"],
                         **({"error": res["error"]} if res["error"] else {})}
        fsutil.save_json_atomic(path, st)
    except Exception:  # noqa: BLE001 - 状态写入失败不影响归档结果
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    d = sys.argv[1] if len(sys.argv) > 1 else None
    r = archive(d, quiet=True)
    print(f"{r['date']}: ok={r['ok']} bytes={r['bytes']} files={r['files']} error={r['error']}")
