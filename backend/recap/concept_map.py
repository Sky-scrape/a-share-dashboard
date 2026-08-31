# -*- coding: utf-8 -*-
"""个股→同花顺概念归属映射（备选池「概念」维度的单一来源，2026-09-04）。

背景：一级行业口径太粗（板块内个股驱动差异大，如电力行业的恒盛能源=培育钻石、
协鑫能科=算电），备选池非涨停组需要概念级别的热度评判。

与 auc_industry（行业映射）同套路：概念目录（ht.catalog cn_concept，约 390 个
885xxx.TI）逐个 index constituents 反查成分，汇总为 by_concept={概念名: [6位代码]}。
产物 data/recap/concept_map.json（ts/built_at/concepts_ok/concepts_total/by_concept）：
- 新鲜度默认 7 天（概念成分漂移慢于行业）；stale 时 load() 返回 {}（消费方诚实降级）；
- 刷新时个别概念抓取失败→沿用该概念旧成员（只按概念粒度合并），失败面 >30% 视为
  上游异常放弃覆盖（防呆：不拿残缺目录静默顶掉完整旧档）；
- 每次实际重建另存一份时点快照 data/recap/concept_map_archive/concept_map_YYYYMMDD.json.gz
  （同日覆盖，保留最近 60 份）——历史验证「按当前成分近似」的边界从此不再扩大：
  未来迭代可用归档序列做时点口径（2026-09-04 起积累，之前的历史区间仍按当前成分近似）。

用法：
    python backend/recap/concept_map.py [--force]   # 手动/每日刷新（fetch_task.bat 调用，
                                                    # build(force=False) 新鲜时 no-op、失败留日志不阻断）
    from concept_map import load                    # 消费方只读映射
"""
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ht  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                    "data", "recap", "concept_map.json")
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                           "data", "recap", "concept_map_archive")
ARCHIVE_KEEP = 60
CACHE_DIR = os.path.join(HERE, ".ht_cache")
MAX_AGE_DAYS = 7
FAIL_RATIO_MAX = 0.30

# 纯机械成分概念（指数入选/股本属性类，成员几百只、涨停家数恒高，没有题材信息量）。
# load() 时过滤，不参与备选池概念热度评判；by_concept 原样保留完整数据。
GENERIC = {
    "融资融券", "转融券标的", "深股通", "沪股通", "标普道琼斯A股", "MSCI中国",
    "富时罗素", "证金持股", "汇金持股", "机构重仓", "基金重仓", "社保重仓",
    "QFII重仓", "保险重仓", "信托重仓", "券商重仓", "预盈预增", "预亏预减",
    "昨日涨停", "昨日连板", "昨日触板", "昨日连板_含一字", "ST股", "次新股",
    "破净股", "低价股", "微盘股", "百元股", "高送转", "送转预期", "股权激励",
    "举牌", "并购重组", "借壳上市", "分拆上市", "定增", "增持回购", "股权转让",
    "沪企改革", "深圳国资改革", "国企改革", "央企改革", "地方国资改革",
}


def _catalog():
    items = ht.catalog("cn_concept", cache_dir=CACHE_DIR)
    out = [(str(x["thscode"]), x["name"]) for x in items]
    if len(out) < 100:
        raise RuntimeError(f"概念目录异常（仅 {len(out)} 个）")
    return out


def _archive(doc):
    """重建成功后存一份时点快照（gzip，同日覆盖，保留最近 ARCHIVE_KEEP 份）。失败不阻断。"""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        day = time.strftime("%Y%m%d")
        fp = os.path.join(ARCHIVE_DIR, f"concept_map_{day}.json.gz")
        tmp = fp + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, fp)
        olds = sorted(f for f in os.listdir(ARCHIVE_DIR)
                      if f.startswith("concept_map_") and f.endswith(".json.gz"))
        for name in olds[:-ARCHIVE_KEEP]:
            try:
                os.remove(os.path.join(ARCHIVE_DIR, name))
            except OSError:
                pass
        return fp
    except Exception:  # noqa: BLE001 - 归档失败不影响主产物
        return None


def build(force=False, max_age_days=MAX_AGE_DAYS, log=None):
    """全量重建；返回 (是否写入, 概念成功数/总数)。force=True 忽略新鲜度。"""
    say = log or (lambda *a: None)
    if not force and os.path.exists(PATH):
        try:
            with open(PATH, encoding="utf-8") as f:
                old = json.load(f)
            if (time.time() - (old.get("ts") or 0)) / 86400.0 < max_age_days:
                return False, (old.get("concepts_ok") or 0, old.get("concepts_total") or 0)
        except Exception:  # noqa: BLE001
            pass
    cat = _catalog()
    old_by_concept = {}
    if os.path.exists(PATH):
        try:
            with open(PATH, encoding="utf-8") as f:
                old_by_concept = json.load(f).get("by_concept") or {}
        except Exception:  # noqa: BLE001
            old_by_concept = {}

    by_concept, failed = {}, []
    for i, (code, name) in enumerate(cat):
        try:
            d = ht.ht("index", "constituents", "--thscode", code, timeout=20)
            items = d.get("item") or []
            codes = sorted({str(m.get("thscode") or "").split(".")[0]
                            for m in items if m.get("thscode")})
            if codes:
                by_concept[name] = codes
            else:
                failed.append(name)
        except Exception:  # noqa: BLE001 - 单概念失败不中断
            failed.append(name)
        if i % 40 == 0:
            say(f"  concept_map {i}/{len(cat)} ok={len(by_concept)} fail={len(failed)}")

    # 失败概念沿用旧成员（成分漂移慢，宁旧勿缺）；失败面过大放弃覆盖
    for name in failed:
        if name in old_by_concept and old_by_concept[name]:
            by_concept[name] = old_by_concept[name]
    if len(failed) > FAIL_RATIO_MAX * len(cat):
        raise RuntimeError(f"概念成分失败面过大 {len(failed)}/{len(cat)}，放弃覆盖")

    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    doc = {"ts": time.time(),
           "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "concepts_total": len(cat),
           "concepts_ok": len(by_concept),
           "by_concept": by_concept}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, PATH)
    _archive(doc)
    return True, (len(by_concept), len(cat))


def load(max_age_days=MAX_AGE_DAYS):
    """{6位代码: [概念名...]}（剔除 GENERIC 机械概念，按概念成员数降序）；缺/旧返回 {}。"""
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        if (time.time() - (d.get("ts") or 0)) / 86400.0 >= max_age_days:
            return {}
        by_concept = d.get("by_concept") or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for name in sorted(by_concept, key=lambda k: -len(by_concept[k])):
        if name in GENERIC:
            continue
        for c in by_concept[name]:
            out.setdefault(c, []).append(name)
    return out


if __name__ == "__main__":
    force = "--force" in sys.argv
    done, (ok, total) = build(force=force, log=print)
    print(f"concept_map 写入={done} 概念 {ok}/{total} -> {PATH}")
