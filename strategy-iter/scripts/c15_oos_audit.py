# -*- coding: utf-8 -*-
"""C15 样本外复审 · 第一步：台账审计 + 修正基线 + 分层/环境诊断（只读）。

背景：C7 样本外 20 个验证日触发 severe（pool_opt.json oos_reviews 20261015）。
本脚本回答三个问题：
  1. 现有验证记录里，哪些「验证日 ↔ 选股池」是错配/重复/漏验的？
  2. 剔除错配后 C7 的真实样本外基线是多少？
  3. 选股日的环境/题材/分数分层里，哪些子集明显更差（供预注册立项）？

严格只读：不写 data/recap，不改生产代码；产物只落 runs/c15_oos_review/。
所有特征取「选股日 T 当日或更早」，无未来函数。
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c15_common as C  # noqa: E402

BASELINE = {"win_rate": 62.09, "avg_close": 2.497}   # 研究窗 C7 基线（spec_validate._OOS_BASELINE）


# ------------------------------------------------------------ 审计（纯函数）

def pool_state(date8):
    """选股日快照的备选池状态：missing_snapshot / missing_module / empty_pool / ok。"""
    if not (C.SNAP_DIR / f"{date8}.json").exists() and \
       not (C.SNAP_DIR / f"{date8}.json.gz").exists():
        return "missing_snapshot", 0
    pool, _ = C.snap_pool(date8)
    if pool is None:
        return "missing_module", 0
    return ("ok" if (pool.get("picks") or []) else "empty_pool"), len(pool.get("picks") or [])


def audit_records(track, calendar):
    """逐验证日核对「应验池日期」与「实验池日期」。

    应验池日期 = 日历中严格早于验证日的最后一个交易日（不用情绪面板近似）。
    status: match / mismatch / no_record（该验证日无台账行）。
    pool_gap: 选股日快照本身缺失或模块缺失（重验的根因候选）。
    """
    validations = track.get("validations") or {}
    v_dates = sorted(k for k, v in validations.items() if (v or {}).get("rules") == "c7")
    if not v_dates:
        return [], [], [], []
    lo, hi = C.iso(v_dates[0]), C.iso(v_dates[-1])
    window = [d for d in calendar if lo <= d <= hi]
    recs = []
    for viso in window:
        d8 = viso.replace("-", "")
        exp = C.prev_trade_day(calendar, viso)
        exp8 = exp.replace("-", "") if exp else None
        rec = validations.get(d8)
        actual = (rec or {}).get("date")
        if rec is None:
            status = "no_record"
        elif exp8 and actual == exp8:
            status = "match"
        else:
            status = "mismatch"
        state, npick = pool_state(exp8) if exp8 else ("no_expected", 0)
        recs.append({
            "validation_date": d8,
            "expected_pool_date": exp8,
            "actual_pool_date": actual,
            "status": status,
            "ok": (rec or {}).get("ok"),
            "n_picks": len((rec or {}).get("picks") or []),
            "pool_state": state,
            "pool_n": npick,
            "env": (rec or {}).get("env"),
        })
    # 重复验证：同一实验池被两个验证日使用
    by_actual = collections.Counter(r["actual_pool_date"] for r in recs
                                    if r["actual_pool_date"])
    dup_actual = {d for d, c in by_actual.items() if c > 1}
    for r in recs:
        r["pool_validated_times"] = by_actual.get(r["actual_pool_date"], 0)
        r["duplicate"] = r["actual_pool_date"] in dup_actual
    # 漏验：应验池日期在所有记录里从未作为 actual 出现。
    # 必须区分「有票却漏验」（样本损失）与「空池未留痕」（无票可验，仅少一条空池标记）
    # ——2026-09-28 属后者：快照 ok=True 但 picks=[]（结论「空仓观察」），无票丢失。
    validated = {r["actual_pool_date"] for r in recs}
    missing_all = [r for r in recs
                   if r["expected_pool_date"] and r["expected_pool_date"] not in validated]
    missing = [r["expected_pool_date"] for r in missing_all if r["pool_state"] == "ok"]
    missing_empty = [r["expected_pool_date"] for r in missing_all
                     if r["pool_state"] in ("empty_pool", "missing_module", "missing_snapshot")]
    return recs, sorted(dup_actual), missing, missing_empty


def clean_rows(track, recs):
    """修正口径样本：仅取「日期严格匹配」记录的票（剔除错配/重验/漏验带入的样本）。"""
    validations = track.get("validations") or {}
    ok_dates = {r["validation_date"] for r in recs if r["status"] == "match"}
    rows = []
    for d8 in sorted(validations):
        if d8 not in ok_dates:
            continue
        v = validations[d8]
        for p in v.get("picks") or []:
            if p.get("close") is None:
                continue
            rows.append(dict(p, validation_date=d8, pool_date=v.get("date"),
                             env=p.get("env") or v.get("env")))
    return rows


def raw_rows(track, rules="c7"):
    """未修正样本（现状口径），用于对照。"""
    rows = []
    for d8, v in sorted((track.get("validations") or {}).items()):
        if (v or {}).get("rules") != rules:
            continue
        for p in v.get("picks") or []:
            if p.get("close") is None:
                continue
            rows.append(dict(p, validation_date=d8, pool_date=v.get("date"),
                             env=p.get("env") or v.get("env")))
    return rows


# ------------------------------------------------------------ 统计（纯函数）

def stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = sum(1 for p in rows if p["close"] > 0)
    return {
        "n": n,
        "win_rate": round(100.0 * wins / n, 2),
        "avg_close": round(sum(p["close"] for p in rows) / n, 3),
        "median_close": round(sorted(p["close"] for p in rows)[n // 2], 3),
        "risk_hit_rate": round(100.0 * sum(1 for p in rows if p.get("risk_hit")) / n, 1),
    }


def by(rows, keyfn, min_n=1):
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[keyfn(r)].append(r)
    return {k: stats(v) for k, v in sorted(buckets.items(), key=lambda kv: str(kv[0]))
            if len(v) >= min_n}


def month_of(r):
    return "2026-" + r["validation_date"][4:6]


def score_band(r):
    s = r.get("pick_score") or 0
    for hi, tag in ((70, "70-79"), (80, "80-84"), (85, "85-89"), (90, "90-94"), (200, "95+")):
        if s < hi:
            return tag
    return "95+"


# ------------------------------------------------------------ 选股日特征

def day_features(date8):
    """选股日 T 当日可得的环境/题材特征（无未来函数）。"""
    sp = C.snap_spec(date8)
    cyc = sp.get("cycle") or {}
    ind = cyc.get("indicators") or {}
    pool, _ = C.snap_pool(date8)
    mkt = (pool or {}).get("market") or {}
    return {
        "phase": cyc.get("phase"),
        "zt": ind.get("zt"),
        "zt_prev3": ind.get("zt_prev3"),
        "max_lb": ind.get("max_lb"),
        "max_lb_prev3": ind.get("max_lb_prev3"),
        "promo": ind.get("promo"),
        "promo_prev3": ind.get("promo_prev3"),
        "sentiment": ind.get("sentiment"),
        "market_cn": mkt.get("情绪判断"),
    }


def theme_features(date8, code, concept):
    """选股日题材簇特征：先按成员代码命中，再退回概念名包含匹配。

    成员代码命中是主路径——题材名（如「微短剧」）与个股所属概念（如「短剧游戏」）
    常不同名，只按名字匹配会大量漏配（初版实测 36 票只识别 4 票）。"""
    sp = C.snap_spec(date8)
    groups = ((sp.get("themes") or {}).get("groups")) or []
    code6 = str(code or "")
    fallback = None
    for g in groups:
        name = str(g.get("题材") or "")
        codes = {str(m.get("代码") or "") for m in (g.get("成员") or [])}
        core = str(((g.get("核心") or {}).get("代码")) or "")
        if code6 and (code6 in codes or code6 == core):
            return {"theme_found": True, "theme": name, "theme_n": g.get("家数"),
                    "theme_h": g.get("高度"), "theme_back": g.get("后排"),
                    "theme_persist": g.get("持续性"), "theme_members": len(codes),
                    "theme_match": "member"}
        if fallback is None and name and concept and (name in str(concept) or str(concept) in name):
            fallback = {"theme_found": True, "theme": name, "theme_n": g.get("家数"),
                        "theme_h": g.get("高度"), "theme_back": g.get("后排"),
                        "theme_persist": g.get("持续性"), "theme_members": len(codes),
                        "theme_match": "name"}
    return fallback or {"theme_found": False, "theme_n": None,
                        "groups_total": len(groups)}


def enrich(rows):
    """给修正口径的票补选股日特征。

    台账行不含 factors/所属概念，需回选股日快照按代码取（同日/同码只解析一次），
    保证读的就是生产当日写下的那份因子，不用验证日信息回填。
    """
    day_cache, pick_cache = {}, {}
    for r in rows:
        pd8 = r.get("pool_date")
        if pd8 not in day_cache:
            pool, _ = C.snap_pool(pd8)
            picks = {}
            for p in (pool or {}).get("picks") or []:
                picks[str(p.get("代码") or "")] = p
            pick_cache[pd8] = picks
            day_cache[pd8] = day_features(pd8)
        src = pick_cache[pd8].get(str(r.get("code") or "")) or {}
        fac = src.get("factors") or {}
        concept = src.get("所属概念") or src.get("核心逻辑") or r.get("concept")
        r["feat"] = {**day_cache[pd8],
                     **theme_features(pd8, r.get("code"), concept),
                     "sector": src.get("所属板块"),
                     "role": src.get("角色"),
                     "lb": src.get("连板")}
        r["fac"] = fac
        r["concept"] = concept
    return rows


# ------------------------------------------------------------ 报告

def _line_stats(label, s):
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — |"
    return (f"| {label} | {s['n']} | {s['win_rate']}% | {s['avg_close']}% | "
            f"{s.get('risk_hit_rate')}% |")


def _table(title, groups):
    out = [f"**{title}**", "", "| 分组 | 样本数 | 收红率 | 平均次日涨幅 | 风险位触发率 |",
           "|---|---:|---:|---:|---:|"]
    for k, s in groups.items():
        out.append(_line_stats(k, s))
    out.append("")
    return out


def main():
    calendar = C.trade_calendar()
    track = C.load_track()
    recs, dup, missing, missing_empty = audit_records(track, calendar)
    clean = enrich(clean_rows(track, recs))
    raw = raw_rows(track)

    # ---------------- audit_report.md
    L = ["# C15 复审 · 台账审计（1/3）：验证日 ↔ 选股池 日期匹配", "",
         f"生成：{C.iso('20261016')}｜数据源：`data/recap/pool_track.json` + 本地行情库交易日历",
         "（`raw_kline_daily` distinct date，不用情绪面板近似）", "",
         "## 1. 结论", ""]
    bad = [r for r in recs if r["status"] != "match"]
    L.append(f"- C7 样本外窗口：{recs[0]['validation_date']} → {recs[-1]['validation_date']}，"
             f"共 {len(recs)} 个验证日。")
    L.append(f"- 日期错配记录：**{len(bad)}** 条；重复使用的池：**{len(dup)}** 个；"
             f"应验却未留痕的选股日：**{len(missing) + len(missing_empty)}** 个"
             f"（其中有票漏验 {len(missing)} 个、空池未留痕 {len(missing_empty)} 个）。")
    L.append(f"- 未修正样本（现状口径）：n={len(raw)}，收红率 "
             f"{stats(raw)['win_rate']}%，平均次日 {stats(raw)['avg_close']}%。")
    L.append(f"- 修正口径（仅日期严格匹配）：n={len(clean)}，收红率 "
             f"{stats(clean)['win_rate']}%，平均次日 {stats(clean)['avg_close']}%。")
    L += ["", "## 2. 逐验证日对照", "",
          "| 验证日 | 应验池 | 实验池 | 状态 | 池状态 | 票数 | 池被验次数 |",
          "|---|---|---|---|---|---:|---:|"]
    for r in recs:
        flag = r["status"] if r["status"] == "match" else f"**{r['status']}**"
        L.append(f"| {r['validation_date']} | {r['expected_pool_date']} | "
                 f"{r['actual_pool_date']} | {flag} | {r['pool_state']} | "
                 f"{r['n_picks']} | {r['pool_validated_times']} |")
    L += ["", "## 3. 问题记录与根因", ""]
    for r in bad:
        L.append(f"- `{r['validation_date']}` 验证日：应验 `{r['expected_pool_date']}`，"
                 f"实际验证 `{r['actual_pool_date']}`（池状态：{r['pool_state']}）。")
    if missing:
        L.append(f"- **有票却漏验**的选股日（样本损失）：{', '.join(missing)}。")
    if missing_empty:
        L.append(f"- 空池未留痕的选股日（无票可验，仅少一条空池标记）：{', '.join(missing_empty)}；"
                 "其快照 `pool.ok=True` 但 `picks=[]`（结论「空仓观察」），"
                 "正确记录应为 ok=False「上一交易日快照无备选池标的」。")
    L += ["", "### 根因", "",
          "`spec_validate.validate_prev_pool` 取「情绪面板中 < 验证日 的最后一行」当上一交易日"
          "（`backend/recap/spec_validate.py:150-154`）。面板缺行时它静默回退到更早的行，"
          "于是拿更早的池当上一交易日池验证——即本次 09-29 重验 09-24 池的机制。",
          "本脚本改用行情库交易日历作唯一日期序列，二者不一致即判错配。", ""]
    L += _table("修正口径 C7 基线（分组）", by(clean, lambda r: r.get("group")))
    C.write_report("audit_report.md", "\n".join(L))

    # ---------------- diagnostics_report.md
    D = ["# C15 复审 · 分层与环境诊断（2/3）", "",
         "样本 = 修正口径 C7 票（日期严格匹配）。特征全部取自**选股日 T 当日或更早**，"
         "无未来函数。收红率 / 平均次日涨幅口径与生产一致（含买点未触发票，"
         "是选股质量口径，不代表可交易收益）。", ""]
    D.append(f"全体：{_line_stats('all', stats(clean)).strip('| ')}".replace("|", "｜"))
    D.append("")
    D += _table("按分组", by(clean, lambda r: r.get("group")))
    D += _table("按环境档", by(clean, lambda r: r.get("env")))
    D += _table("按月份", by(clean, month_of))
    D += _table("按得分段", by(clean, score_band))
    D += _table("按周期阶段（选股日）", by(clean, lambda r: r["feat"].get("phase")))
    D += _table("按情绪判断（选股日）", by(clean, lambda r: r["feat"].get("market_cn")))

    def zt_trend(r):
        f = r["feat"]
        zt, p3 = f.get("zt"), f.get("zt_prev3")
        if zt is None or not p3:
            return "未知"
        return "涨停扩张" if zt > p3 * 1.1 else ("涨停收缩" if zt < p3 * 0.8 else "涨停持平")

    def promo_trend(r):
        f = r["feat"]
        pr, p3 = f.get("promo"), f.get("promo_prev3")
        if pr is None or not p3:
            return "未知"
        return "晋级率升" if pr > p3 else "晋级率降"

    D += _table("按涨停家数趋势（选股日 vs 前3日均）", by(clean, zt_trend))
    D += _table("按晋级率趋势", by(clean, promo_trend))
    D += _table("按题材簇是否识别", by(clean, lambda r: "识别" if r["feat"].get("theme_found") else "未识别"))
    D += _table("按题材簇家数", by(clean, lambda r: f"家数{min(int(r['feat'].get('theme_n') or 0), 5)}"
                                   if r["feat"].get("theme_found") else "未识别"))

    # 混杂检查：最可疑的分层是否只是月份/环境的代理
    def confound(title, pred):
        sel = [r for r in clean if pred(r)]
        other = [r for r in clean if not pred(r)]
        out = [f"**混杂检查 · {title}**（命中 n={len(sel)}）", "",
               "| 命中子集分布 | 命中 | 未命中 |", "|---|---:|---:|"]
        for name, fn in (("2026-10 占比", lambda rr: "10月" if month_of(rr) == "2026-10" else "9月"),
                         ("环境档", lambda rr: rr.get("env")),
                         ("分组", lambda rr: rr.get("group")),
                         ("周期阶段", lambda rr: rr["feat"].get("phase"))):
            a = collections.Counter(fn(r) for r in sel)
            b = collections.Counter(fn(r) for r in other)
            for k in sorted(set(a) | set(b), key=str):
                pa = f"{a.get(k,0)}/{len(sel)}" if sel else "0"
                pb = f"{b.get(k,0)}/{len(other)}" if other else "0"
                out.append(f"| {name}={k} | {pa} | {pb} |")
        out.append("")
        return out

    D += confound("选股日涨停扩张（zt > 前3日均×1.1）",
                  lambda r: zt_trend(r) == "涨停扩张")
    D += confound("得分 95+", lambda r: score_band(r) == "95+")
    D += confound("进攻档", lambda r: r.get("env") == "aggressive")

    # 分桶标签（factors.buckets 在台账里是 buckets 字段）
    def bucket_tags(r):
        return r.get("buckets") or []

    tag_rows = collections.defaultdict(list)
    for r in clean:
        for t in bucket_tags(r):
            tag_rows[t].append(r)
    D += _table("按分桶标签（一票多标签，分别统计）",
                {k: stats(v) for k, v in sorted(tag_rows.items()) if len(v) >= 3})

    def concept_lu_band(r):
        v = (r.get("fac") or {}).get("concept_lu")
        if v is None:
            return "缺失"
        return f"概念涨停{min(int(v), 3)}"

    D += _table("题材线索因子（快照 factors.concept_lu）", by(clean, concept_lu_band))
    D += _table("按所属板块抱团（快照 factors.sec）",
                by(clean, lambda r: f"板块涨停{min(int((r.get('fac') or {}).get('sec') or 0), 4)}"))
    D += _table("按比值甜区位置（factors.ratio60）",
                by(clean, lambda r: ("低于甜区" if (r.get("fac") or {}).get("ratio60", 1) < 0.86
                                     else "高于甜区" if (r.get("fac") or {}).get("ratio60", 0) > 0.95
                                     else "甜区内")
                   if (r.get("fac") or {}).get("ratio60") is not None else "非低吸组"))
    D += ["### 归因分布（收跌票）", ""]
    attr = collections.Counter(r.get("attribution") for r in clean if r["close"] <= 0)
    D.append("| 归因 | 票数 |")
    D.append("|---|---:|")
    for k, v in attr.most_common():
        D.append(f"| {k} | {v} |")
    D.append("")
    C.write_report("diagnostics_report.md", "\n".join(D))

    # ---------------- 汇总 json
    C.write_json("audit.json", {
        "records": recs, "duplicate_pools": dup, "missing_pool_dates": missing,
        "missing_empty_pool_dates": missing_empty,
        "raw": stats(raw), "clean": stats(clean),
        "clean_by_group": by(clean, lambda r: r.get("group")),
        "clean_by_env": by(clean, lambda r: r.get("env")),
        "clean_by_month": by(clean, month_of),
        "clean_by_phase": by(clean, lambda r: r["feat"].get("phase")),
    })
    print(f"[c15] audit: {len(bad)} bad, dup={dup}, missing={missing}, "
          f"missing_empty={missing_empty}")
    print(f"[c15] raw   n={len(raw)} win={stats(raw)['win_rate']} avg={stats(raw)['avg_close']}")
    print(f"[c15] clean n={len(clean)} win={stats(clean)['win_rate']} avg={stats(clean)['avg_close']}")
    print(f"[c15] out   {C.OUT_DIR}")


if __name__ == "__main__":
    main()
