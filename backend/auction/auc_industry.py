# -*- coding: utf-8 -*-
"""个股 → 一级行业全量映射（消灭「未分类」的供给侧）。

背景：观察池行业标注原来只来自复盘快照里涨停池行自带的「所属行业」，
热股榜行没有行业字段 → 纯热股来源的股票全部落「未分类」。
本模块用同花顺 90 个一级行业指数（881xxx）的成分股反查，一次构建全市场映射，
之后低频刷新（成分/行业归属很少变）。

实测契约：index.constituents 无分页参数（schema paging=none），单次调用返回全部成分
（半导体 881121 返回 187 条，未截断）。若上游哪天改成截断返回，这里没有 total 可比对，
只能靠「映射总数是否明显缩水」在体检/日志里发现，所以 rebuild 会打印并留存行数。

产出（原子写）：
    data/auction/industry_map.json  {"built_at", "date8", "industries": N, "stocks": M,
                                     "map": {thscode: 行业名}}
直跑：python auc_industry.py            强制全量重建
      python auc_industry.py --status   只看现状
"""
import json
import os
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend", "recap"))

import ht                       # noqa: E402
import auc_config as C          # noqa: E402

MAP_NAME = "industry_map.json"
MAX_AGE_DAYS = 3               # 超过即重建；新股迟早进一级行业成分，3 天兼顾新股缺口与调用量


def load_map():
    """读本地映射；文件缺失/损坏返回 {}（调用方按缺标注处理，不抛错）。"""
    d = C.load_json(MAP_NAME) or {}
    m = d.get("map")
    return m if isinstance(m, dict) else {}


def age_days():
    d = C.load_json(MAP_NAME) or {}
    ts = d.get("ts")
    return (time.time() - ts) / 86400.0 if ts else None


def need_rebuild():
    d = C.load_json(MAP_NAME) or {}
    if not d.get("map") or not d.get("ts"):
        return True
    return (time.time() - d["ts"]) / 86400.0 > MAX_AGE_DAYS


def rebuild():
    """90 个一级行业逐个拉成分，构建 {thscode: 行业名}。失败抛 RuntimeError。"""
    cat = ht.catalog("industry", cache_dir=C.DATA_DIR)
    t1 = [x for x in cat if str(x.get("thscode", "")).startswith("881")]
    if len(t1) < 60:   # 目录异常缩水时拒绝覆盖好数据
        raise RuntimeError(f"一级行业目录只有 {len(t1)} 个，疑似上游异常，放弃重建")
    mapping, dup = {}, 0
    for i, x in enumerate(t1):
        code, name = x["thscode"], x.get("name") or x["thscode"]
        d = ht.ht("index", "constituents", "--thscode", code, timeout=120)
        for row in (d.get("item") or []):
            tc = row.get("thscode")
            if not tc:
                continue
            if tc in mapping:
                dup += 1        # 一股票理论只属一个一级行业；重复保留先见的即可
                continue
            mapping[tc] = name
        time.sleep(0.2)         # 温和限速，90 个行业不值得打爆上游
        if (i + 1) % 15 == 0:
            print(f"industry_map: {i + 1}/{len(t1)} 行业，已映射 {len(mapping)} 只", flush=True)
    if len(mapping) < 4000:     # A 股全市场 5000+，明显偏小说明成分大面积拉取失败
        raise RuntimeError(f"映射仅 {len(mapping)} 只，疑似成分接口截断/失败，不落盘")
    C.save_json(MAP_NAME, {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "ts": time.time(),
                           "date8": time.strftime("%Y%m%d"),
                           "industries": len(t1), "stocks": len(mapping),
                           "dup": dup, "map": mapping})
    print(f"industry_map: 重建完成 {len(t1)} 行业 / {len(mapping)} 只（重复归属 {dup}）", flush=True)
    return len(mapping)


def maybe_refresh():
    """供 collector 终态路径调用：到期则重建并刷新 watchmap。

    失败只记日志——行业映射是锦上添花，绝不能拖死主采集流程。
    """
    if not need_rebuild():
        return None
    try:
        rebuild()
    except Exception as e:  # noqa: BLE001
        print(f"industry_map: 重建失败（保留旧数据，下轮再试）: {e}", file=sys.stderr)
        return None
    try:
        C.build_watchlist()   # 立即回填 watchmap，不等明日采集
    except Exception as e:  # noqa: BLE001
        print(f"industry_map: watchmap 刷新失败: {e}", file=sys.stderr)
    return "industry_map rebuilt + watchmap refreshed"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--status" in sys.argv:
        d = C.load_json(MAP_NAME) or {}
        print(json.dumps({k: d.get(k) for k in
                          ("built_at", "industries", "stocks", "dup")}, ensure_ascii=False))
        wm = C.load_json("watchmap.json") or {}
        no = [tc for tc, e in wm.items() if not (e.get("i") or (d.get("map") or {}).get(tc))]
        print("watchmap 仍无行业的代码:", len(no), no[:5])
    else:
        rebuild()
