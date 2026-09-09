# -*- coding: utf-8 -*-
"""投机分析计算引擎 —— 盘后复盘新板块（2026-09-02）。

把用户的投机方法论文档（文案.doc，摘要见 docs/speculation-notes.md）
映射为可计算数据，输出快照模块 `speculation`：

  themes    题材结构·核心识别：涨停池按涨停原因首段聚类；核心 = 连板最高
            （平手时：封板时间最早 → 封单最大）；后排数衡量持续性
            （文案：后排越多的核心持续性越好；独苗题材高度有限）
  ladder    连板梯队（按板高分组，供「连板天梯」视角）
  deviation 偏离值雷达：本地 DuckDB 全市场扫描（v_daily_qfq 十年日线）。
            口径（文案「严重异常波动」章节）：偏离值 = 个股区间涨幅 − 指数区间涨幅；
            普通异动 = 3 日累计偏离 ±20%（10cm）/ ±30%（20cm）；
            严重异动 = 10 日偏离 ≥100% 或 30 日偏离 ≥200%（10cm/20cm 同）。
            基准指数：沪市→上证指数 000001.SH，深市→深证成指 399001.SZ
            （index.history 全段缓存于 .ht_cache，不重复抓）。
  fate      高位承接 / A杀监控：昨日连板≥2 个股今日结局
            （连板晋级/炸板/断板收涨/断板走弱/断板大跌/跌停），
            非池内个股今日涨跌幅来自 DuckDB；按板高分组统计承接率
            （文案：中位股、补涨股最容易出现 A杀）
  cycle     情绪周期定位：启动-发酵-高潮-震荡-退潮 的规则化机械推断
            （涨停数/最高连板/晋级率 近3日 vs 前3日），只给依据不下结论
  anomalies 异动/监管事件：special anomaly-list（today-only，历史日期诚实置空）
  rules     静态口径速查：监管阶梯（抓捕＞封账户＞停牌＞限制买入＞公告）、
            异动阈值、核心交易要点（摘自用户文案）
  pool      明日交易备选池（C7 主板概念口径）：四档环境配额 + 涨停组/低吸组打分
            + 负面清单 + 昨日一字排除；C6 起环境分档含隔夜美股闸门（纳指/标普
            隔夜 ≤-2.0% 强制 defensive），C7 起入选门槛 70 + 市场量能闸门
            （缩量不低吸；expB/expC 单变量实验采纳，expA 否决；T+2 持有证伪）；
            资金管理层 S1（防守/冰点档半仓）为建议层；
            每日验证自我优化闭环——
            validate_prev_pool 用 T 日真实走势验证 T-1 的 picks（买点/风险位/归因，
            冻结执行层 _MF_BUY），record_validation 按日留痕 pool_track.json，
            update_optimizer 从 45 日滚动窗口统计有界调整（因子分桶惩罚 +
            入选门槛微调，状态为验证记录的纯函数，幂等可重放）→ pool_opt.json；
            结构规则/环境配额/执行层永不自动改动，样本不足不动作

数据源：本地快照（调用方传 bundles）+ 本地 DuckDB + index.history + anomaly-list。
HTTP 层不做业务计算：providers.speculation() 在每日抓取时调用本引擎；
本文件也可独立回填既有快照：

    python backend/recap/speculate.py --date 20260902    # 单日计算并合并进快照
    python backend/recap/speculate.py --recent 30        # 回填最近 30 份快照
    python backend/recap/speculate.py --missing          # 回填所有缺失快照
"""
import argparse
import csv
import datetime as _dt
import gzip
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ht          # noqa: E402
import snapio      # noqa: E402
import thscodes    # noqa: E402  代码转换单一来源（backend/thscodes.py）
import concept_map  # noqa: E402  个股→概念归属（备选池概念维度单一来源）
import us_market   # noqa: E402  隔夜美股因子单一来源（backend/us_market.py）
from modules import cget  # noqa: E402
import industry_common  # noqa: E402  # backend/ 个股→一级行业共享映射读取器

VERSION = 1

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".ht_cache")
PANEL_SENTIMENT = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "data", "recap", "panel", "sentiment.csv")
POOL_TRACK = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                          "data", "recap", "pool_track.json")   # 每日验证记录（按验证日覆盖写）
POOL_OPT = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "data", "recap", "pool_opt.json")       # 优化器状态（验证记录的纯函数）

# M_Final 执行层（冻结）：买点/风险位判定口径（与最终筛选方案-主板.md 一致）。
# 验证用，不随优化器变化——优化器只调选股侧，不调执行侧。
# 常量单一来源 backend/execution_layer.py（竞价页执行卡共用同一份窗口）。
import execution_layer  # noqa: E402
_MF_BUY = execution_layer.MF_BUY
# 优化器有界旋钮：门槛上下限与惩罚上限（结构规则永不自动改动）
_OPT_MIN_SCORE = {"lu": (60.0, 75.0), "nlu": (60.0, 75.0)}
_OPT_WINDOW_DAYS = 45      # 滚动统计窗口（自然日）
_OPT_BUCKET_MIN_N = 8      # 分桶惩罚最少样本
_OPT_MINSCORE_MIN_N = 20   # 门槛调整最少样本

# ---- 口径常量（文案「严重异常波动」章节）----
SEVERE_D10 = 100.0          # 严重异动：10 日偏离 ≥100%
SEVERE_D30 = 200.0          # 严重异动：30 日偏离 ≥200%
NORMAL_D3 = {"10cm": 20.0, "20cm": 30.0}   # 普通异动：3 日累计偏离
NEAR_D10 = 80.0             # 「逼近严重」提示线（80% 阈值）
NEAR_D30 = 160.0
SCAN_TH = "(r10 >= 0.6 OR r30 >= 1.2 OR r3 >= 0.15)"  # DuckDB 预筛（全市场）

INDEX_BENCH = {
    "SH": ("000001.SH", "上证指数"),
    "SZ": ("399001.SZ", "深证成指"),
}

# 监管手段从严到松（文案原文）
RULES = {
    "监管阶梯": ["抓捕", "封账户", "停牌", "限制买入", "公告"],
    "异动口径": {
        "普通异动": "连续 3 个交易日累计偏离值 ±20%（10cm）/ ±30%（20cm）",
        "严重异动": "10 天偏离值 ≥100% 或 30 天偏离值 ≥200%（10cm/20cm 同）",
        "偏离值": "区间内个股涨幅 − 区间内指数涨幅",
        "本页基准": "沪市个股对上证指数，深市个股对深证成指",
    },
    "核心交易": [
        "有总龙做总龙：做最确定的、地位最高的那个",
        "核心调整的时候，风险就随之来临",
        "逆指数核心的震荡期最佳卖点：指数下跌而它涨不动时",
        "核心被新周期核心卡位、形成跷跷板时，是卖点",
        "核心震荡期不追涨杀跌：大涨抛、跳水低吸",
        "震荡期利好容易高开低走，不幻想再走一波主升",
        "新龙拉升导致老龙跳水（同在老龙周期内）：低吸老核心的时机",
        "被总核心带着走的票：总核心封板而带不动时是卖点，冲高出",
    ],
    "核心结束": [
        "加速即买盘枯竭：巨量阴线就是短期顶部",
        "多数核心死于监管，注意监管的空间和尺度",
        "后排大跌的板块：核心即便震荡也不应持有（补跌风险）",
        "新龙出现、人气衰退：周期结束",
    ],
    "走弱信号": [
        "走弱的原因是地位减弱，不是利空",
        "失去带动性就不算核心：被反推到涨停反而是卖点",
        "多次低开高走 / 连续低开是弱的表现（无利空低开尤其警惕）",
    ],
    "双头情形": [
        "有地位的核心不会 A杀：大主线首次回调后多有双头机会",
        "市场真空期会选择有辨识度的老龙",
        "新题材高潮 + 老题材调整到位 → 资金切回老题材辨识度标的",
        "龙回头后再无预期：除非新周期被证伪",
    ],
    "一字纪律": [
        "一字顶上去的核心筹码断层、风险巨大（前排全一字时板块风险巨大）",
        "一字上去起码经历一次换手才比较保险",
        "地位越低的一字涨停风险越大：中位股是大面源泉",
    ],
}


# ---------------------------------------------------------------- 基础工具

def _date_iso(date8):
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"


def board_class(code6):
    """10cm / 20cm / bj（北交所 30cm 不在文案口径内，单列不判定）。"""
    c = str(code6)
    if c.startswith("30"):
        return "20cm"          # 创业板 300/301/302
    if c.startswith("68"):
        return "20cm"          # 科创板 688/689
    if c.startswith(("8", "4", "92")):
        return "bj"
    return "10cm"


def thscode_of(code6):
    """6 位代码 → thscode（带市场后缀）。单一来源 backend/thscodes.py。"""
    return thscodes.to_thscode(code6)


def _round2(x):
    return None if x is None else round(float(x), 2)


# ---------------------------------------------------------------- 指数基准

def index_series(market, need_date8):
    """基准指数日线 [[date, close]...]（升序），本地缓存，覆盖不足才重抓。"""
    code, _name = INDEX_BENCH[market]
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, "index_hist_" + code.replace(".", "_") + ".json")
    need = _date_iso(need_date8)
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                saved = json.load(f)
            rows = saved.get("rows") or []
            if rows and rows[-1][0] >= need:
                return rows
        except Exception:  # noqa: BLE001 - 缓存坏就重抓
            pass
    start8 = (_dt.datetime.strptime(need, "%Y-%m-%d")
              - _dt.timedelta(days=1500)).strftime("%Y%m%d")
    d = ht.ht("index", "history", "--thscode", code,
              "--start-ms", str(ht.date_ms(start8)),
              "--end-ms", str(ht.date_ms(need_date8) + 86400 * 1000),
              timeout=60)
    rows = [[_dt.datetime.fromtimestamp(r["date_ms"] / 1000).strftime("%Y-%m-%d"),
             float(r["close_price"])]
            for r in (d.get("item") or []) if r.get("close_price")]
    if not rows:
        raise RuntimeError(f"index.history {code} 为空")
    Path(fp).write_text(
        json.dumps({"ts": time.time(), "code": code, "rows": rows}),
        encoding="utf-8")
    return rows


def index_gain(rows, date_iso, n):
    """指数 n 个交易日区间涨幅（%）；当日或窗口不足返回 None。"""
    pos = [i for i, (d, _c) in enumerate(rows) if d <= date_iso]
    if not pos:
        return None
    i = pos[-1]
    if i - n < 0:
        return None
    return (rows[i][1] / rows[i - n][1] - 1) * 100.0


_ROT_BOARDS = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                           "data", "rotation", "boards.json")


def _industry_catalog():
    """行业名 → 881 代码（data/rotation/boards.json 全站单一目录）；缺失返回 {}。"""
    try:
        with open(_ROT_BOARDS, encoding="utf-8") as f:
            d = json.load(f)
        out = {}
        for b in (d.get("industry") or []):
            code = str(b.get("code") or "").split(".")[0]
            if code.startswith("881") and b.get("name"):
                out[b["name"]] = code
        return out
    except Exception:  # noqa: BLE001
        return {}


def _industry_lookup():
    """{code6: (ind_code881, 行业名)}；共享映射（≤5 天新鲜）× 轮动目录，缺一返回 {}。"""
    names = industry_common.load_shared_ticker_map()
    if not names:
        return {}
    cat = _industry_catalog()
    if not cat:
        return {}
    out = {}
    for code6, name in names.items():
        ic = cat.get(name)
        if ic:
            out[code6] = (ic, name)
    return out


def industry_series(ind_code, need_date8):
    """行业指数日线 [[date, close]...]（升序，.ht_cache 按 881 代码缓存，增量补尾）。

    与 index_series 同源（hithink index.history，thscode 形如 881101.TI），但行业
    按需取用：每日验证只碰备选池标的所属行业，首次全量回溯 1500 天、之后只补
    缓存尾段之后的新数据；缓存带 checked_through——非交易日拿不到新数据时不再
    对同一 need 日反复请求。
    """
    code = str(ind_code).split(".")[0]
    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, f"index_hist_{code}.json")
    need = _date_iso(need_date8)
    rows, checked = [], ""
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                saved = json.load(f)
            rows = saved.get("rows") or []
            checked = saved.get("checked_through") or ""
        except Exception:  # noqa: BLE001 - 缓存坏就重抓
            rows, checked = [], ""
    if rows and (rows[-1][0] >= need or checked >= need):
        return rows
    if rows:
        start_ms = ht.date_ms(rows[-1][0].replace("-", "")) + 86400 * 1000
    else:
        start8 = (_dt.datetime.strptime(need, "%Y-%m-%d")
                  - _dt.timedelta(days=1500)).strftime("%Y%m%d")
        start_ms = ht.date_ms(start8)
    d = ht.ht("index", "history", "--thscode", code + ".TI",
              "--start-ms", str(start_ms),
              "--end-ms", str(ht.date_ms(need_date8) + 86400 * 1000), timeout=60)
    new = [[_dt.datetime.fromtimestamp(r["date_ms"] / 1000).strftime("%Y-%m-%d"),
            float(r["close_price"])]
           for r in (d.get("item") or []) if r.get("close_price")]
    merged = {r[0]: r[1] for r in rows}
    for dd, cc in new:
        merged[dd] = cc
    rows = [[k, v] for k, v in sorted(merged.items())]
    Path(fp).write_text(
        json.dumps({"ts": time.time(), "code": code, "rows": rows,
                    "checked_through": need}),
        encoding="utf-8")
    return rows


def _series_pct(rows, date_iso):
    """行业指数 date_iso 当日涨幅（%）；该日不在序列内（未覆盖/停市）返回 None。"""
    idx = [i for i, (d, _c) in enumerate(rows) if d <= date_iso]
    if not idx:
        return None
    i = idx[-1]
    if i == 0 or rows[i][0] != date_iso:
        return None
    c0, c1 = rows[i - 1][1], rows[i][1]
    return round((c1 / c0 - 1) * 100.0, 2) if c0 else None


# ---------------------------------------------------------------- DuckDB

def _db_export(sql, tag):
    """db export 到 .ht_cache 相对路径，读回 ndjson 行（CLI 只认相对路径）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_abs = os.path.join(CACHE_DIR, f"{tag}.ndjson")
    out_rel = os.path.relpath(out_abs, os.getcwd())
    try:
        ht.ht("db", "export", "--sql", sql, "--output", out_rel, timeout=180)
        rows = []
        with open(out_abs, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    finally:
        try:
            os.remove(out_abs)
        except OSError:
            pass


def scan_deviation(date8, extra_thscodes=()):
    """全市场偏离值候选扫描（单日）。返回 [{thscode, r1, r3, r10, r30}]。"""
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=80)).strftime("%Y-%m-%d")
    in_sql = ""
    codes = sorted(set(extra_thscodes))
    if codes:
        in_sql = " OR thscode IN (" + ", ".join(f"'{c}'" for c in codes) + ")"
    sql = (
        "WITH w AS (SELECT thscode, date, close, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r1, "
        "close/NULLIF(LAG(close,3) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r3, "
        "close/NULLIF(LAG(close,10) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r10, "
        "close/NULLIF(LAG(close,30) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r30 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}') "
        "SELECT thscode, ROUND(r1*100,2) r1, ROUND(r3*100,2) r3, "
        "ROUND(r10*100,2) r10, ROUND(r30*100,2) r30 "
        f"FROM w WHERE date = DATE '{d}' AND ({SCAN_TH}{in_sql})"
    )
    return _db_export(sql, f"devscan_{date8}")


def day_pcts(date8, thscodes):
    """指定个股当日涨跌幅（%）：{thscode: pct}。"""
    if not thscodes:
        return {}
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=15)).strftime("%Y-%m-%d")
    in_sql = ", ".join(f"'{c}'" for c in sorted(set(thscodes)))
    sql = (
        "WITH w AS (SELECT thscode, date, close, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS r1 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        f"SELECT thscode, ROUND(r1*100,2) pct FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"fatepct_{date8}")
    return {r["thscode"]: r.get("pct") for r in rows}


# ---------------------------------------------------------------- 题材与梯队

def _first_theme(reason):
    s = str(reason or "").strip()
    if not s:
        return "未分类"
    return s.split("+")[0].strip() or "未分类"


def build_themes(zt_rows):
    """按涨停原因首段聚类；核心 = 连板最高（封板时间最早 → 封单最大 平手裁决）。

    返回 {groups, summary}：summary 给题材分散度一个诚实的量化描述
    （文案：大题材分支多各有核心；独苗/分散市场没有合力）。
    """
    groups = {}
    for r in zt_rows:
        groups.setdefault(_first_theme(r.get("涨停原因")), []).append(r)
    out = []
    for theme, rows in groups.items():
        rows = sorted(rows, key=lambda r: (
            -(int(r.get("连板数") or 1)),
            str(r.get("首次封板时间") or "99:99"),
            -(float(r.get("封板资金") or 0)),
        ))
        core = rows[0]
        back = len(rows) - 1
        out.append({
            "题材": theme, "家数": len(rows),
            "高度": max(int(r.get("连板数") or 1) for r in rows),
            "后排": back,
            "持续性": ("独苗题材·无后排" if back == 0 else
                       "后排充足" if back >= 4 else "有后排"),
            "核心": {
                "代码": str(cget(core, "code") or ""),
                "名称": cget(core, "name"),
                "连板": int(core.get("连板数") or 1),
                "封板时间": core.get("首次封板时间"),
                "封板资金": core.get("封板资金"),
                "涨停原因": core.get("涨停原因"),
                "is_st": bool(core.get("is_st")),
            },
            "成员": [{"代码": str(cget(r, "code") or ""),
                       "名称": cget(r, "name"),
                       "连板": int(r.get("连板数") or 1)} for r in rows],
        })
    out.sort(key=lambda t: (-t["高度"], -t["家数"], t["题材"]))
    total = len(zt_rows)
    top3 = sum(t["家数"] for t in out[:3])
    biggest = out[0] if out else None
    if not total:
        verdict = "当日无涨停"
    elif biggest and biggest["家数"] >= 8:
        verdict = "题材效应集中·有主线雏形"
    elif biggest and biggest["家数"] >= 3:
        verdict = "题材偏分散·局部有合力"
    else:
        verdict = "极度分散·以独立逻辑为主"
    summary = {
        "题材数": len(out), "涨停总数": total,
        "最大题材": (biggest["题材"] if biggest else None),
        "最大题材家数": (biggest["家数"] if biggest else 0),
        "前三题材覆盖": top3,
        "前三占比": round(top3 / total * 100) if total else None,
        "定性": verdict,
    }
    return {"groups": out, "summary": summary}


def build_ladder(zt_rows):
    by_lb = {}
    for r in zt_rows:
        lb = int(r.get("连板数") or 1)
        by_lb.setdefault(lb, []).append({
            "代码": str(cget(r, "code") or ""), "名称": cget(r, "name"),
            "原因": _first_theme(r.get("涨停原因")),
        })
    rows = [{"板数": k, "家数": len(v), "个股": v}
            for k, v in sorted(by_lb.items(), reverse=True)]
    return {"rows": rows, "max_lb": (rows[0]["板数"] if rows else 0)}


# ---------------------------------------------------------------- 偏离值雷达

def _dev_status(board, dev3, dev10, dev30):
    if board == "bj":
        return "bj"
    if ((dev10 is not None and dev10 >= SEVERE_D10)
            or (dev30 is not None and dev30 >= SEVERE_D30)):
        return "severe"
    if ((dev10 is not None and dev10 >= NEAR_D10)
            or (dev30 is not None and dev30 >= NEAR_D30)):
        return "near"
    th = NORMAL_D3.get(board, 20.0)
    if dev3 is not None and abs(dev3) >= th:
        return "normal"
    return "watch"


def build_deviation(date8, zt_rows, zb_rows):
    """偏离值雷达：全市场扫描 + 池内个股强制入选。失败抛异常由上层降级。"""
    pool = {}
    for r in (zt_rows or []):
        pool[str(cget(r, "code") or "")] = ("zt", r)
    for r in (zb_rows or []):
        c = str(cget(r, "code") or "")
        if c and c not in pool:
            pool[c] = ("zb", r)
    extra = [thscode_of(c) for c in pool]

    rows = scan_deviation(date8, extra_thscodes=extra)
    iso = _date_iso(date8)
    gains = {}
    for mkt in ("SH", "SZ"):
        series = index_series(mkt, date8)
        gains[mkt] = {
            "code": INDEX_BENCH[mkt][0], "name": INDEX_BENCH[mkt][1],
            "g3": index_gain(series, iso, 3),
            "g10": index_gain(series, iso, 10),
            "g30": index_gain(series, iso, 30),
        }

    name_map = None   # 懒加载（仅池外个股需要）
    out = []
    for row in rows:
        thscode = str(row.get("thscode") or "")
        code6 = thscode.split(".")[0]
        mkt = thscode.split(".")[-1]
        board = board_class(code6)
        g = gains.get(mkt)
        if g is None:      # 北交所无基准：保留行但不判定
            dev3 = dev10 = dev30 = None
        else:
            dev3 = (None if row.get("r3") is None or g["g3"] is None
                    else row["r3"] - g["g3"])
            dev10 = (None if row.get("r10") is None or g["g10"] is None
                     else row["r10"] - g["g10"])
            dev30 = (None if row.get("r30") is None or g["g30"] is None
                     else row["r30"] - g["g30"])
        space10 = None if dev10 is None else max(SEVERE_D10 - dev10, 0)
        space30 = None if dev30 is None else max(SEVERE_D30 - dev30, 0)
        spaces = [s for s in (space10, space30) if s is not None]
        status = _dev_status(board, dev3, dev10, dev30)

        hit = pool.get(code6)
        name, lb, theme, is_st = None, None, None, False
        if hit:
            src, pr = hit
            name = cget(pr, "name")
            lb = int(pr.get("连板数") or 0) if src == "zt" else None
            theme = _first_theme(pr.get("涨停原因"))
            is_st = bool(pr.get("is_st"))
        if name is None:
            if name_map is None:
                name_map = ht.symbol_names(cache_dir=CACHE_DIR)
            name = name_map.get(code6) or code6
        if not name:
            name = code6

        out.append({
            "代码": code6, "名称": name, "thscode": thscode, "board": board,
            "今涨": row.get("r1"), "连板": lb, "题材": theme,
            "in_zt": code6 in pool and pool[code6][0] == "zt",
            "in_zb": code6 in pool and pool[code6][0] == "zb",
            "is_st": is_st or "ST" in str(name).upper(),
            "r3": row.get("r3"), "r10": row.get("r10"), "r30": row.get("r30"),
            "dev3": _round2(dev3), "dev10": _round2(dev10), "dev30": _round2(dev30),
            "space10": _round2(space10), "space30": _round2(space30),
            "空间": _round2(min(spaces)) if spaces else None,
            "status": status,
        })

    # 排序：严重 > 逼近 > 普通 > 观察 > 北交所（不在判定口径内，沉底）；
    # 同状态内按「最接近哪条严重线」降序。bj 与 watch 分属不同 rank，
    # 不能用单一分值混排（量纲不同会把北交所顶到最前）。
    _STATUS_RANK = {"severe": 0, "near": 1, "normal": 2, "watch": 3, "bj": 4}

    def _sev(x):
        rank = _STATUS_RANK.get(x["status"], 5)
        if x["status"] == "bj":
            score = 0.0
        else:
            score = max(
                (x["dev10"] if x["dev10"] is not None else -999) / SEVERE_D10,
                (x["dev30"] if x["dev30"] is not None else -999) / SEVERE_D30)
        return (rank, -score)

    out.sort(key=_sev)
    return {"benchmark": gains, "rows": out[:80], "total": len(out)}


# ---------------------------------------------------------------- 高位承接

_FATE_ORDER = {"连板晋级": 0, "炸板": 1, "断板收涨": 2, "断板走弱": 3,
               "断板大跌": 4, "跌停": 5, "未知": 6}


def build_fate(date8, prev_zt_rows, bundles):
    """昨日连板≥2 个股今日结局。DuckDB 失败仅影响今日涨跌幅列。"""
    if not prev_zt_rows:
        return {"rows": [], "stats": [], "note": "昨日快照无涨停池，无法计算承接"}
    zt_today = {str(cget(r, "code") or "") for r in (bundles.get("zt") or [])}
    zb_today = {str(cget(r, "code") or "") for r in (bundles.get("zb") or [])}
    dt_today = {str(cget(r, "code") or "") for r in (bundles.get("dt") or [])}

    cands = [r for r in prev_zt_rows if int(r.get("连板数") or 1) >= 2]
    if not cands:
        return {"rows": [], "stats": [], "note": "昨日无 2 连板以上个股"}

    pcts = {}
    try:
        pcts = day_pcts(date8, [thscode_of(str(cget(r, "code") or ""))
                                for r in cands])
        pcts = {k.split(".")[0]: v for k, v in pcts.items()}
    except Exception as e:  # noqa: BLE001 - 涨跌幅缺失不阻断结局分类
        note = f"（DuckDB 涨跌幅不可用: {type(e).__name__}）"
    else:
        note = ""

    rows = []
    for r in cands:
        code = str(cget(r, "code") or "")
        pct = pcts.get(code)
        if code in zt_today:
            outcome = "连板晋级"
        elif code in dt_today:
            outcome = "跌停"
        elif code in zb_today:
            outcome = "炸板"
        elif pct is None:
            outcome = "未知"
        elif pct <= -5:
            outcome = "断板大跌"
        elif pct < 0:
            outcome = "断板走弱"
        else:
            outcome = "断板收涨"
        rows.append({
            "代码": code, "名称": cget(r, "name"),
            "昨连板": int(r.get("连板数") or 1),
            "题材": _first_theme(r.get("涨停原因")),
            "今涨": pct, "结局": outcome,
        })
    rows.sort(key=lambda x: (x["昨连板"], -_FATE_ORDER.get(x["结局"], 9)),
              reverse=True)

    stats = []
    by_lb = {}
    for x in rows:
        by_lb.setdefault(x["昨连板"], []).append(x)
    for lb in sorted(by_lb, reverse=True):
        xs = by_lb[lb]
        pcts_ok = [x["今涨"] for x in xs if x["今涨"] is not None]
        up_n = sum(1 for x in xs
                   if x["结局"] == "连板晋级" or (x["今涨"] or -99) > 0)
        stats.append({
            "板数": lb, "家数": len(xs),
            "晋级": sum(1 for x in xs if x["结局"] == "连板晋级"),
            "红盘率": round(up_n / len(xs) * 100) if xs else None,
            "均涨": _round2(sum(pcts_ok) / len(pcts_ok)) if pcts_ok else None,
            "大跌": sum(1 for x in xs if x["结局"] in ("断板大跌", "跌停")),
        })
    return {"rows": rows, "stats": stats, "note": note or None}


# ---------------------------------------------------------------- 情绪周期

def read_sentiment_rows():
    """sentiment.csv → [{date, index, zt, dt, max_lb, promo_rate, ...}] 升序。"""
    rows = []
    if not os.path.exists(PANEL_SENTIMENT):
        return rows
    with open(PANEL_SENTIMENT, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = str(r.get("date") or "").strip()
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"   # csv 是 8 位日期，统一成 ISO 再比较

            def num(k):
                try:
                    v = r.get(k)
                    return None if v in (None, "", "None") else float(v)
                except (TypeError, ValueError):
                    return None
            rows.append({
                "date": d, "index": num("index"),
                "label": r.get("label"), "zt": num("zt"), "dt": num("dt"),
                "max_lb": num("max_lb"), "promo_rate": num("promo_rate"),
                "up_ratio": num("up_ratio"), "zhaban": num("zhaban"),
            })
    rows.sort(key=lambda x: x["date"] or "")
    return rows


def _avg(key, rs):
    vals = [r[key] for r in rs if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def build_cycle(date8, sent_rows):
    """启动-发酵-高潮-震荡-退潮 的规则化定位（近3日 vs 前3日，机械推断）。"""
    iso = _date_iso(date8)
    rows = [r for r in sent_rows if (r["date"] or "") <= iso]
    if len(rows) < 8:
        return {"phase": "数据不足", "reasons": ["情绪序列不足 8 个交易日"],
                "series": rows[-30:], "indicators": {}}
    cur = rows[-1]
    caveat = None if cur["date"] == iso else f"注：{iso} 无情绪数据，以 {cur['date']} 为最新"
    p3 = rows[-6:-3]

    zt_now, zt_p3 = cur["zt"], _avg("zt", p3)
    lb_now, lb_p3 = cur["max_lb"], _avg("max_lb", p3)
    pr_now, pr_p3 = cur["promo_rate"], _avg("promo_rate", p3)
    lb_win = [r["max_lb"] for r in rows[-30:] if r["max_lb"] is not None]

    reasons = []
    if zt_now is not None and zt_p3 is not None:
        reasons.append(f"涨停 {int(zt_now)} 家（前3日均 {zt_p3:.0f}）")
    if lb_now is not None and lb_p3 is not None:
        reasons.append(f"最高 {int(lb_now)} 板（前3日均 {lb_p3:.1f}）")
    if pr_now is not None and pr_p3 is not None:
        reasons.append(f"晋级率 {pr_now*100:.0f}%（前3日均 {pr_p3*100:.0f}%）")

    phase = "震荡"
    if zt_now is not None and lb_now is not None and zt_now >= 80 and lb_now >= 6:
        phase = "高潮"
    elif zt_now is not None and zt_now <= 25:
        phase = "冰点"
    elif (lb_now is not None and lb_p3 is not None and lb_now < lb_p3 - 0.7) or \
            (pr_now is not None and pr_p3 is not None
             and pr_now < pr_p3 - 0.12
             and zt_now is not None and zt_p3 is not None and zt_now < zt_p3):
        phase = "退潮"
    elif (lb_now is not None and lb_p3 is not None and lb_now > lb_p3 + 0.5
          and zt_now is not None and zt_p3 is not None and zt_now >= zt_p3):
        phase = "发酵"
    if phase == "冰点" and zt_now is not None and zt_p3 is not None \
            and zt_now > zt_p3 + 5:
        phase = "启动"
    if lb_win:
        reasons.append(f"30日高度区间 {int(min(lb_win))}–{int(max(lb_win))} 板")
    if caveat:
        reasons.append(caveat)

    return {
        "phase": phase, "reasons": reasons,
        "indicators": {
            "zt": zt_now, "zt_prev3": zt_p3, "max_lb": lb_now,
            "max_lb_prev3": lb_p3, "promo": pr_now, "promo_prev3": pr_p3,
            "sentiment": cur["index"],
        },
        "series": [{"date": r["date"], "zt": r["zt"], "max_lb": r["max_lb"],
                    "promo": r["promo_rate"], "idx": r["index"]}
                   for r in rows[-30:]],
    }


# ---------------------------------------------------------------- 异动事件

def build_anomalies(historical):
    if historical:
        return {"available": False,
                "note": "异动榜单为当日接口（today-only），历史日期不可回补"}
    d = ht.ht("special", "anomaly-list", timeout=90)
    rows = []
    for r in (d.get("item") or []):
        content = str(r.get("analysis_content") or "")
        summary = content.split("（免责声明")[0].strip()
        if len(summary) > 220:
            summary = summary[:220] + "…"
        thscode = str(r.get("thscode") or "")
        rows.append({
            "代码": thscode.split(".")[0], "名称": r.get("stock_name"),
            "标签": r.get("tag_name"),
            "关键词": r.get("keyword_list") or [],
            "摘要": summary,
        })
    return {"available": True, "rows": rows[:40], "total": len(rows)}


# ---------------------------------------------------------------- 明日交易备选池

# ---------------------------------------------------------------- 明日备选池（C_Final 主板概念口径）
# 依据 strategy-iter/reports/最终筛选方案-主板概念.md（C1→C3 三轮完整迭代收敛，C3=C_Final；
# 执行层/环境分档/主板范围沿用 M_Final 冻结口径，选股评分引入概念一阶因子）：
# 选股范围仅沪深主板（沪 600/601/603/605、深 000/001/002/003），创业板/科创板/北交所不入选；
# 研究层（题材涨停计数等）仍用全市场。四档环境配额 + 涨停组/非涨停组加权打分
# + 负面清单硬排除 + 选股与买点分离。数据边界近似见各注释。

_MF_QUOTA = {"aggressive": (2, 0), "normal": (2, 2), "defensive": (2, 1), "freeze": (0, 1)}
_MF_ENV_CN = {"aggressive": "进攻", "normal": "正常", "defensive": "防守", "freeze": "冰点"}
# C6 隔夜美股闸门（strategy-iter us_round1_C4→us_round3_C6 三轮完整迭代收敛）：
# 纳指综合/标普500(SPY) 隔夜跌幅 ≤-2.0% 强制 defensive。与 engine rules.C6 同口径。
_MF_US_GATE = (-2.0, -2.0)


def _us_row(date8):
    """T 日隔夜美股因子行（factors.json；us_date=最近一个在 T 09:15 前收盘的美股
    交易日）。数据缺失返回 None——闸门不启用（诚实降级，与 engine 断言口径互补）。"""
    try:
        rows = us_market.load_factors().get("rows") or {}
        return rows.get(_date_iso(date8))
    except Exception:  # noqa: BLE001
        return None

_MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def _is_main_board(code6):
    """选股范围（M_Final 起冻结）：仅沪深主板（002/003 中小板已并入深主板）。"""
    return str(code6).startswith(_MAIN_PREFIX)


def _prev_yizi_codes(date8, sent_rows):
    """上一交易日一字板代码集合（open==high==low==close，DuckDB 前复权口径）。

    负面清单（M_Final 起冻结）：昨日一字板不接力。无上一交易日数据时返回 None（不启用该排除）。
    """
    iso = _date_iso(date8)
    prevs = [r["date"] for r in sent_rows if (r["date"] or "") < iso]
    if not prevs:
        return None
    try:
        rows = _db_export(
            "SELECT thscode FROM v_daily_qfq WHERE date = DATE '" + prevs[-1] + "' "
            "AND open = high AND high = low AND low = close", f"prevyizi_{date8}")
    except Exception:  # noqa: BLE001 - 一字排除失败不阻断选股
        return None
    return {str(r.get("thscode") or "").split(".")[0] for r in rows}


_ROT_DAILY = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                          "data", "rotation", "daily")

# C7 非涨停组市场量能闸门（expC 实验：缩量日 NLU -0.24% n=47 / 放量日 +2.13% n=41）
_NLU_AMT_RATIO_MIN = 0.9


def _market_amt_ratio(date8):
    """T 日全市场成交额 / 前 19 个交易日均值（DuckDB 单查询）。

    与 engine ms.amt_ratio 同口径（T 日收盘可得，无未来函数）；数据缺失返回
    None——闸门不启用（诚实降级）。"""
    iso = _date_iso(date8)
    try:
        rows = _db_export(
            "SELECT date, SUM(amount) AS amt FROM v_daily_qfq "
            f"WHERE date <= DATE '{iso}' GROUP BY date ORDER BY date DESC LIMIT 20",
            f"amtratio_{date8}")
    except Exception:  # noqa: BLE001
        return None
    amts = [r.get("amt") for r in rows if r.get("amt")]
    if len(amts) < 20 or not amts[0]:
        return None
    base = sum(amts[1:]) / 19.0
    return amts[0] / base if base else None


def _rotation_strength(date8, zt_codes):
    """板块强度（非涨停组板块因子真实口径，M_Final 起冻结）：轮动日线 90 个一级行业。

    返回 (stats, meta)：stats = {881代码: {name, pct(当日%), pct5(5日累计%),
    rank(当日涨幅分位), rank5(5日分位), lu_cnt(板块今日涨停家数)}}；分位口径与
    strategy-iter 引擎一致（当日全行业升序百分位，平分 长度）。meta.as_of 为实际
    取到的交易日（date8 当日文件缺失时取最近一日并标 stale）；轮动数据整体缺失
    （如 2026-07 之前的历史回填日）返回 (None, None)——必须是 None 而非 {}，
    _nlu_candidate 靠 `ind_stats is not None` 区分「真实口径但个股无归属」与
    「轮动数据整体缺失」，后者才能退回龙虎榜代理口径。
    """
    try:
        files = sorted(f[:-5] for f in os.listdir(_ROT_DAILY)
                       if f.endswith(".json") and len(f) == 15)
    except OSError:
        return None, None
    iso = _date_iso(date8)
    hist = [d for d in files if d <= iso]
    if not hist:
        return None, None
    as_of = hist[-1]
    window = hist[-6:]   # 当日 + 前 5 个交易日（5 日累计 = 近 5 个日涨幅复合）

    def _last_pct(day):
        try:
            with open(os.path.join(_ROT_DAILY, day + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for code, v in (d.get("series") or {}).items():
            ps = [p for p in (v.get("pcts") or []) if isinstance(p, (int, float))]
            if ps:
                out[code.split(".")[0]] = ps[-1]
        return out

    days = [_last_pct(d) for d in window]
    # 空文件跳过回退：最新一日可能是采集竞态下的半截文件（如复盘 17:05 任务与轮动
    # 17:10 兑底任务同时写盘）或采集断流日——只认有真实数据的最近一日，避免整池
    # 无谓退回代理口径；仍无任何数据才放弃。
    last_good = None
    for i in range(len(days) - 1, -1, -1):
        if days[i]:
            last_good = i
            break
    if last_good is None:
        return None, None
    cur = days[last_good]
    as_of = window[last_good]
    pct5 = {}
    codes = sorted(cur)
    hist5 = days[:last_good + 1][-5:]
    for c in codes:
        prod, n = 1.0, 0
        for day in hist5:
            p = day.get(c)
            if p is None:
                continue
            prod *= 1 + p / 100.0
            n += 1
        pct5[c] = (prod - 1) * 100.0 if n >= 2 else None

    def _rank(dct, x):
        vals = sorted(v for v in dct.values() if v is not None)
        if not vals or x is None:
            return None
        less = sum(1 for v in vals if v < x)
        eq = sum(1 for v in vals if v == x)
        return (less + eq * 0.5) / len(vals)

    name_by_code = {c: n for n, c in _industry_catalog().items()}
    ind_by = _industry_lookup()   # code6 → (881, 行业名)
    lu_cnt = {}
    for tc in set(zt_codes or ()):
        ind = ind_by.get(str(tc))
        if ind:
            lu_cnt[ind[0]] = lu_cnt.get(ind[0], 0) + 1
    stats = {}
    for c in codes:
        stats[c] = {"name": name_by_code.get(c, c), "pct": cur[c], "pct5": pct5.get(c),
                    "rank": _rank(cur, cur[c]), "rank5": _rank(pct5, pct5.get(c)),
                    "lu_cnt": lu_cnt.get(c, 0)}
    return stats, {"as_of": as_of, "stale": as_of != iso, "n": len(stats)}


def _lu_volr(date8, codes):
    """涨停池个股当日量比（volume / 含当日前5日均量，strategy-iter 引擎同口径）。"""
    if not codes:
        return {}
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=20)).strftime("%Y-%m-%d")
    in_sql = ", ".join("'" + thscode_of(c) + "'" for c in sorted(set(codes)))
    sql = (
        "WITH w AS (SELECT thscode, date, volume, "
        "AVG(volume) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) v5 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        "SELECT thscode, ROUND(volume/NULLIF(v5,0),2) volr "
        f"FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"luvolr_{date8}")
    return {str(r.get("thscode") or "").split(".")[0]: r.get("volr") for r in rows}


def _concept_heat(zt_codes, cmap, concept_rows):
    """概念热度三表：涨停池按概念归属计数 + 概念指数当日涨幅/排名（快照 concepts 模块）。

    cmap 为 {} （concept_map 缺失/过期）时涨停计数为空表——概念维度诚实降级。
    """
    lu_by, pct_by, rank_by = {}, {}, {}
    for tc in set(zt_codes or ()):
        for name in (cmap.get(str(tc)) or []):
            lu_by[name] = lu_by.get(name, 0) + 1
    for r in (concept_rows or []):
        name = r.get("概念")
        if name:
            pct_by[name] = r.get("涨跌幅")
            rank_by[name] = r.get("涨幅排名")
    return lu_by, pct_by, rank_by


def _best_concept(code6, cmap, lu_by, pct_by):
    """个股驱动概念：概念指数当日涨幅优先（市场今天在交易的概念），涨停家数次之。

    不按涨停家数优先——大概念（如新能源汽车）成员多、家数恒高，会盖住真实驱动
    （电力行业里协鑫能科是算电/AIDC、恒盛能源是培育钻石，靠涨幅才分得开）。
    返回 (概念名, 概念内涨停家数, 概念当日涨幅)。"""
    best, best_key = None, (-999.0, -1)
    for name in (cmap.get(code6) or []):
        p = pct_by.get(name)
        key = (p if p is not None else -999.0, lu_by.get(name, 0))
        if key > best_key:
            best_key, best = key, name
    if best is None:
        return None, 0, None
    return best, best_key[1], best_key[0]


# C_Final 概念热度档（strategy-iter C1→C3 轮末证据，概念一阶因子主梯度）：
# 概念内涨停家数 —— 非涨停组概念 <=2 家桶均次 -1.37%（n=46，四个概念涨幅桶内全负）
# vs >=3 家 +0.92%（n=116，全正）。档位 10/4/3/2 = 引擎 recog_concept_tiers ×10。
_CONCEPT_LU_TIERS = ((3, 10), (2, 4), (1, 3), (0, 2))

# C_Final 非涨停组行业当日涨幅分位下限（行业为辅防线下限）：
# ind_rank<0.6 桶连续两轮负期望（C1 -1.07% n=16 / C2 -1.88% n=18）。
_NLU_SECTOR_RANK_MIN = 0.6


def _concept_score(lu, pct=None):
    """概念热度分（0~10，C_Final 家数阶梯）：概念内涨停 >=3 家 10 / 2 家 4 / 1 家 3 / 0 家 2。

    pct 仅留痕（C_Final 家数优先，涨幅梯度跨轮漂移判噪声、不作档位依据）；
    lu 为 None（概念数据缺失）返回 0，由调用方回退龙虎榜/热股口径。"""
    for thr, sc in _CONCEPT_LU_TIERS:
        if (lu or 0) >= thr:
            return sc
    return 0


def _concept_gate(code6, cmap, lu_by, pct_by):
    """概念独狼闸门字段（C_Final 涨停组，系统文档六.0）：(全部概念走弱, 概念内最大涨停家数)。

    个股全部所属概念当日收跌且概念内均无第二家涨停 -> 按独狼板排除；
    无任何概念当日有行情时返回 None（概念数据缺失，闸门不启用——诚实降级）。"""
    pcts, max_lu = [], 0
    for n in (cmap.get(code6) or []):
        p = pct_by.get(n)
        if p is None:
            continue
        pcts.append(p)
        max_lu = max(max_lu, lu_by.get(n, 0))
    if not pcts:
        return None
    return (all(p < 0 for p in pcts), max_lu)


def scan_trend(date8):
    """非涨停组趋势/位置/动量/流动性全市场扫描（DuckDB 单日，主板范围在 Python 侧过滤）。

    返回 [{thscode, pct, close, ma10, ma20, ma20p, ma60, high60, gain20, volr, amount, amt5}]。
    硬筛对应非涨停组（M_Final 起冻结）：当日涨幅 -3~+7%、多头排列（close>ma20>ma60*0.995）、
    上市 ≥120 根 bar（400 日窗口内计数）、成交 ≥10 亿。
    """
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=400)).strftime("%Y-%m-%d")
    sql = (
        "WITH w1 AS (SELECT thscode, date, close, volume, amount, "
        "COUNT(*) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) bars, "
        "close/NULLIF(LAG(close,1) OVER (PARTITION BY thscode ORDER BY date),0)-1 AS pct, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) ma10, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) ma20, "
        "AVG(close) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) ma60, "
        "MAX(high) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) high60, "
        "LAG(close,20) OVER (PARTITION BY thscode ORDER BY date) c20, "
        "AVG(volume) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) vol5, "
        "AVG(amount) OVER (PARTITION BY thscode ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) amt5 "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}'), "
        "w2 AS (SELECT *, LAG(ma20,1) OVER (PARTITION BY thscode ORDER BY date) ma20p FROM w1) "
        "SELECT w2.thscode, s.name, ROUND(pct*100,2) pct, close, ma10, ma20, ma20p, ma60, high60, "
        "ROUND((close/NULLIF(c20,0)-1)*100,2) gain20, ROUND(volume/NULLIF(vol5,0),2) volr, amount, amt5 "
        f"FROM w2 JOIN v_symbol s USING (thscode) WHERE date = DATE '{d}' AND w2.thscode NOT LIKE '%.BJ' "
        "AND bars >= 120 AND amount >= 1000000000 AND pct BETWEEN -0.03 AND 0.07 "
        "AND close > ma20 AND ma20 > ma60 * 0.995"
    )
    return _db_export(sql, f"trendscan_{date8}")


def _mf_env(sent_rows, date8, us_row=None):
    """四档环境分档（M_Final 冻结 + C6 隔夜美股闸门），返回 (env, cur, prev_lb)。

    aggressive：涨停 ≥75 且上涨占比 ≥0.52 且跌停 ≤15；freeze：涨停 <25 且上涨占比 <0.30；
    defensive：跌停 ≥30（恐慌闸门）或涨停 <45 或上涨占比 <0.40；高位崩塌（昨日 ≥5 板且
    今日骤降 ≥2）时 aggressive 降级 normal。C6 新增：隔夜纳指/标普 ≤-2.0% 强制
    defensive（排在恐慌闸门之后、分类之前，与 engine rules.C6 同口径）。
    上证 5 日闸门在 M 线未启用（engine idx5_force_defensive=None），idx5 仅作展示。
    """
    iso = _date_iso(date8)
    rows = [r for r in sent_rows if (r["date"] or "") <= iso]
    if not rows:
        return "normal", {}, None
    cur = rows[-1]
    zt, up, dt = cur.get("zt"), cur.get("up_ratio"), cur.get("dt")
    lb = cur.get("max_lb")
    prev = rows[-2]["max_lb"] if len(rows) >= 2 else None
    if zt is None or up is None:
        return "normal", cur, prev
    if (dt or 0) >= 30:
        env = "defensive"
    elif _us_gate_hit(us_row):
        env = "defensive"
    elif zt >= 75 and up >= 0.52 and (dt or 0) <= 15:
        env = "aggressive"
    elif zt < 25 and up < 0.30:
        env = "freeze"
    elif zt < 45 or up < 0.40:
        env = "defensive"
    else:
        env = "normal"
    if env == "aggressive" and prev is not None and lb is not None \
            and prev >= 5 and (prev - lb) >= 2:
        env = "normal"
    return env, cur, prev


def _us_gate_hit(us_row):
    """隔夜美股闸门判定（纳指/标普 ≤ _MF_US_GATE）；数据缺失不启用。"""
    if not us_row:
        return False
    ndx, spx = us_row.get("纳斯达克综合"), us_row.get("标普500")
    if ndx is not None and ndx <= _MF_US_GATE[0]:
        return True
    return spx is not None and spx <= _MF_US_GATE[1]


def _load_opt():
    """读优化器状态（pool_opt.json）；缺失/损坏/旧线版本回默认（结构规则不受影响）。

    C7（2026-09-09）起版本门收紧到 C7 前缀：旧 C_Final 线旋钮（门槛 55）不带入——
    C7 入选门槛基准 70（expB 实验证据：60~70 分区间 5 只全亏、>=70 占 98%），
    优化器界限同步放宽到 60~75。"""
    base = {"version": "C7_Final", "updated": None,
            "min_score": {"lu": 70.0, "nlu": 70.0}, "penalties": {"lu": {}, "nlu": {}},
            "oos_reviews": [], "log": []}
    try:
        with open(POOL_OPT, encoding="utf-8") as f:
            d = json.load(f)
        ver = str(d.get("version") or "")
        if not ver.startswith("C7"):
            return base   # C_Final 旧线旋钮不带入 C7 线
        base["version"] = ver
        base["updated"] = d.get("updated")
        ms = d.get("min_score") or {}
        base["min_score"] = {"lu": float(ms.get("lu") or 70.0),
                             "nlu": float(ms.get("nlu") or 70.0)}
        pn = d.get("penalties") or {}
        base["penalties"] = {"lu": dict(pn.get("lu") or {}), "nlu": dict(pn.get("nlu") or {})}
        base["oos_reviews"] = list(d.get("oos_reviews") or [])
    except Exception:  # noqa: BLE001 - 无状态文件=默认口径
        pass
    return base


def _pick_buckets(f):
    """因子 → 可惩罚分桶键（与 M3 轮末分析口径一致；优化器与验证共用）。"""
    out = []
    if not f:
        return out
    if f.get("group") == "lu":
        ft = str(f.get("ft") or "99:99")
        if ft > "11:30":
            out.append("time_late")        # 13:30 后封板（M1 实测 +0.39%/-0.25%）
        r = f.get("seal_ratio") or 0
        if r < 0.08:
            out.append("seal_low")         # 封单比 <8%
        lb = int(f.get("lb") or 1)
        if lb == 1:
            out.append("ladder_1")         # 首板（M3 +2.42% 全组最弱档）
        tc = int(f.get("tc") or 2)
        if tc <= 2:
            out.append("theme_2")          # 题材内仅 2 家
        if not f.get("sealed"):
            out.append("struct_reopen")    # 盘中开过板
    else:
        rh = f.get("ratio60") or 0
        if rh >= 0.90:
            out.append("pos_high")         # 贴近 0.95 上限（甜区内偏高位）
        g20 = f.get("g20")
        if g20 is None or g20 < 5:
            out.append("mom_low")          # 20 日涨幅 <5%
        if not f.get("sec"):
            out.append("sector_none")      # 板块代理全缺
        if (f.get("amount") or 0) > 8e9:
            out.append("liq_big")          # 流动性 6 分档（>80 亿）
        if not f.get("net"):
            out.append("recog_none")       # 无龙虎榜净买
        if f.get("concept_lu") is not None and f.get("concept_lu") < 3:
            out.append("concept_cold")     # 概念温吞（概念内涨停<3家，C_Final 主梯度弱档）
    return out


def _lu_candidate(r, theme_count, yizi_prev, pen_lu, ms_lu, volr_map=None, cmap=None, heat=None):
    """涨停组单只打分（C_Final 权重：概念15+梯队20+时间20+封单15+结构15+流动性10+题材5）。

    C_Final（strategy-iter C1→C3 收敛，概念为主、行业为辅）：概念分 = 概念内涨停家数
    阶梯（>=4 家 15 / 3 家 12 / 2 家 9 / <2 家且概念收涨 6 / 冷 3 / 数据缺失 4.5 中低档）；
    题材因子降为交叉校验（15→5，M1/M2 已证零梯度）；封单 20→15 腾权重。
    结构分按量比档位（0.8~3.0→15 / 3~4→10.5 / <0.8→9 / >4→6.75，高位独狼再减半）。
    概念独狼排除（C_Final 新增）：全部所属概念当日走弱且概念内均无第二家涨停 -> 排除
    （与题材独狼并列；概念数据缺失时该排除不启用）。
    负面清单（ST/次新/非主板/昨日一字/成交<1.5亿/尾盘板>14:45/独狼板）或低于
    入选门槛返回 None；否则返回候选 dict。"""
    code = str(cget(r, "code") or "")
    if not code or r.get("is_st") or r.get("is_new") or not _is_main_board(code):
        return None
    if yizi_prev and code in yizi_prev:
        return None   # 昨日一字板不接力（负面清单）
    amt = float(r.get("成交额") or 0)
    seal = float(r.get("封板资金") or 0)
    ft = str(r.get("首次封板时间") or "99:99")
    lb = int(r.get("连板数") or 1)
    theme = _first_theme(r.get("涨停原因"))
    tc = theme_count.get(theme, 0)
    ratio = seal / amt if amt else 0
    # 负面清单：成交额<1.5亿 / 尾盘板(>14:45) / 独狼板(题材内涨停<2家)
    if amt < 1.5e8 or ft >= "14:45" or tc < 2:
        return None
    lu_by, pct_by = (heat[0], heat[1]) if heat else ({}, {})
    c_name, c_lu, c_pct = _best_concept(code, cmap or {}, lu_by, pct_by)
    gate = _concept_gate(code, cmap or {}, lu_by, pct_by)
    if gate and gate[0] and gate[1] <= 1:
        return None   # 概念独狼（C_Final）：全部概念走弱且概念内无第二家涨停
    s_theme = min(max(tc - 1, 0), 5)   # 交叉校验（M1/M2 零梯度，C_Final 降为 5 分）
    if c_name:
        s_concept = 15 if c_lu >= 4 else 12 if c_lu == 3 else 9 if c_lu == 2 \
            else (6 if (c_pct or 0) > 0 else 3)
    else:
        s_concept = 4.5   # 概念数据缺失：中低档（不奖励不惩罚到位）
    s_ladder = 20 if lb in (3, 4) else 18 if lb == 2 else 11 if lb == 1 else 12  # ≥5板 0.6 档
    s_seal = 15 if ratio >= 0.15 else 10.5 if ratio >= 0.08 else 6 if ratio >= 0.03 else 2.25
    s_time = 20 if ft <= "09:45" else 17 if ft <= "10:30" else 14 if ft <= "11:30" \
        else 10 if ft <= "13:30" else 6 if ft <= "14:30" else 1
    s_liq = 10 if 3e8 <= amt <= 1e10 else 6 if amt >= 1.5e8 else 3
    vr = (volr_map or {}).get(code)
    if vr is None:
        s_struct = 7.5   # 量比缺失给中低档（不硬排）
    else:
        s_struct = 15.0
        for lo, hi, tv in ((0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 999.0, 0.45)):
            if lo <= vr < hi:
                s_struct = 15.0 * tv
                break
    if lb >= 4 and tc <= 2:
        s_struct *= 0.5   # 高位独狼加速惩罚（M3 保留）
    fac = {"group": "lu", "ft": ft, "seal_ratio": ratio, "lb": lb, "tc": tc,
           "sealed": str(r.get("最后封板时间")) == ft, "amount": amt, "volr": vr}
    fac["concept"] = c_name
    fac["concept_lu"] = c_lu if c_name else None
    fac["concept_pct"] = c_pct if c_name else None
    score = round(s_theme + s_concept + s_ladder + s_seal + s_time + s_liq + s_struct
                  - sum(float(pen_lu.get(b) or 0) for b in _pick_buckets(fac)), 1)
    if score < ms_lu:
        return None
    return {"code": code, "name": cget(r, "name"), "lb": lb,
            "theme": theme, "score": score, "price": r.get("最新价"),
            "role": "核心龙头" if lb >= 2 else "题材先锋", "nonzt": False,
            "factors": fac, "concepts_top": c_name}


def _nlu_candidate(m, theme_count, zt_codes, lhb_by, hot_codes, pen_nlu, ms_nlu,
                   ind_stats=None, ind_by=None, cmap=None, heat=None):
    """非涨停组单只打分（C_Final 权重：板块25+位置25+趋势20+辨识10+动量10+流动性10）。

    板块因子真实口径（轮动日线 90 行业）：活跃度门槛（当日涨幅分位≥0.8 或板块当日
    有涨停 或 5日分位≥0.85，引擎同口径）+ sector_mix 连续打分 25×(0.55×rank +
    0.25×rank5 + 0.2×min(涨停家数/2,1))；行业归属走共享映射，无归属不选。
    ind_stats 为 None（轮动数据整体缺失的历史回填日）时退回龙虎榜原因代理口径。
    概念维度（C_Final，2026-09-04 起一阶因子）：辨识度 = 概念内涨停家数阶梯
    （>=3 家 10 / 2 家 4 / 1 家 3 / 0 家 2）——C1→C3 轮末证据：概念 <=2 家桶
    -1.37%（n=46）vs >=3 家 +0.92%（n=116），家数是主梯度；映射缺失时诚实降级回
    龙虎榜口径。行业分位下限 _NLU_SECTOR_RANK_MIN=0.6（ind_rank<0.6 桶两轮负期望）。
    硬筛（回调甜区/20日涨幅/量比/多头排列/成交额）不过的返回 None。"""
    code6 = str(m.get("thscode") or "").split(".")[0]
    if not code6 or code6 in zt_codes or not _is_main_board(code6):
        return None
    if "ST" in str(m.get("name") or "").upper():
        return None   # ST 以名称近似（数据边界）
    close = m.get("close") or 0
    high60 = m.get("high60") or 0
    ratio60 = close / high60 if high60 else 0
    g20 = m.get("gain20")
    volr = m.get("volr")
    ma10 = m.get("ma10") or 0
    if not (0.86 <= ratio60 <= 0.95):      # M_Final 回调低吸甜区（两端规避）
        return None
    if g20 is None or not (0 <= g20 <= 30):
        return None
    if volr is None or not (0.35 <= volr <= 1.2):
        return None
    if not (m.get("ma20") or 0) > (m.get("ma20p") or 0):
        return None
    if not ((m.get("amount") or 0) >= 1e9 and (m.get("amt5") or 0) >= 3e8):
        return None
    lr = lhb_by.get(code6)
    net = float(lr.get("龙虎榜净买额") or 0) if lr else 0
    ind = (ind_by or {}).get(code6)
    ist = ind_stats.get(ind[0]) if (ind_stats and ind) else None
    if ind_stats is not None and ist is None:
        return None   # 行业归属/板块数据缺失不选（引擎同口径，宁缺勿错）
    if ist is not None:
        # 真实板块口径：活跃度门槛 + sector_mix 连续打分
        if not ((ist["rank"] or 0) >= 0.8 or ist["lu_cnt"] >= 1 or (ist["rank5"] or 0) >= 0.85):
            return None   # M_Final 板块活跃度门槛
        if (ist["rank"] or 0) < _NLU_SECTOR_RANK_MIN:
            return None   # C_Final 行业分位下限：ind_rank<0.6 桶连续两轮负期望（行业为辅防线）
        s_sector = round(25 * (0.55 * (ist["rank"] or 0) + 0.25 * (ist["rank5"] or 0)
                               + 0.2 * min(ist["lu_cnt"] / 2.0, 1.0)), 1)
        theme = ind[1]          # 所属板块 = 真实一级行业
        sec_lu = ist["lu_cnt"]
    else:
        # 代理口径（历史回填日轮动数据缺失）：题材有涨停近似板块热度
        theme = _first_theme(lr.get("上榜原因")) if lr else None
        sec = theme_count.get(theme, 0) if theme else 0
        s_sector = 25 if sec >= 2 else 16 if sec >= 1 else 10
        sec_lu = sec
    s_pos = 25 if ratio60 < 0.85 else 21 if ratio60 < 0.90 else 15
    s_trend = 20 if (ma10 and close > ma10) else 16   # 多头排列+MA20上行 已由 SQL 硬筛保证
    s_mom = 10 if g20 >= 5 else 7
    s_liq = 10 if 5e8 <= (m.get("amount") or 0) <= 8e9 else 6
    # 概念维度（C_Final 家数阶梯，概念一阶因子主梯度）：概念内涨停 >=3 家 10 /
    # 2 家 4 / 1 家 3 / 0 家 2；概念数据缺失回退龙虎榜/热股口径（不编造概念热度）
    lu_by, pct_by = (heat[0], heat[1]) if heat else ({}, {})
    c_name, c_lu, c_pct = _best_concept(code6, cmap or {}, lu_by, pct_by)
    lhb_score = 10 if net > 0 else 8 if code6 in hot_codes else 0
    s_recog = _concept_score(c_lu if c_name else None, c_pct) if c_name \
        else max(lhb_score, 3)
    fac = {"group": "nlu", "ratio60": ratio60, "g20": g20, "volr": volr,
           "amount": m.get("amount") or 0, "sec": sec_lu, "net": net,
           "concept": c_name, "concept_lu": c_lu if c_name else None,
           "concept_pct": c_pct if c_name else None}
    score = round(s_sector + s_pos + s_trend + s_mom + s_liq + s_recog
                  - sum(float(pen_nlu.get(b) or 0) for b in _pick_buckets(fac)), 1)
    if score < ms_nlu:
        return None
    return {"code": code6, "name": m.get("name"), "lb": 0, "theme": theme or "未分类",
            "score": score, "price": None, "pct": m.get("pct"), "net": net,
            "role": "非涨停·缩量回调低吸", "nonzt": True, "factors": fac,
            "concepts_top": c_name}


def _pool_pick_row(c):
    """候选 → 快照输出行（买点/风险位文案为冻结执行层，不随优化漂移）。"""
    nz = c.get("nonzt")
    stars = max(1, min(5, 5 if c["score"] >= 85 else 4 if c["score"] >= 72
                      else 3 if c["score"] >= 62 else 2))
    if nz:
        trig = "开盘 -2%~+3% 之间 且收盘站上max(开盘,昨收) 才买；开盘超窗放弃"
        zone = f"观察区间 开盘 -2%~+3%（今日 {c.get('pct', 0)}%·缩量回调低吸型）"
        riskline = "盘中最低≤昨收-4% 或收盘≤昨收-3.5% 离场；板块次日熄火无条件走"
        logic = "非涨停·缩量回调低吸（多头排列·距60日高点0.86~0.95甜区·20日涨幅温和·成交≥10亿）"
        if c.get("concepts_top"):
            logic += f"·概念[{c['concepts_top']}]"
        if c.get("net") and c["net"] > 0:
            logic += "；龙虎榜净买入"
    else:
        trig = "开盘 0~+5% 之间 且盘中最低≥昨收-3% 且收盘高于开盘 才买；低开不接、高开>+5%放弃"
        zone = (f"观察区间 开盘 0~+5%（≈{c['price']} 元今收盘）" if c["price"] else
                "观察区间 开盘 0~+5%")
        riskline = "盘中最低≤昨收-5% 或收盘≤昨收-4% 离场；防高开回落/题材断板"
        logic = f"{c['role']}·{c['theme']}·{c['lb']}连板"
        if c.get("concepts_top"):
            logic += f"·概念[{c['concepts_top']}]"
    return {
        "代码": c["code"], "名称": c["name"], "所属板块": c["theme"],
        "所属概念": c.get("concepts_top") or "-",
        "类型": "非涨停板" if nz else "涨停板",
        "角色": c["role"], "连板": c["lb"], "得分": c["score"],
        "核心逻辑": logic,
        "入选强度": stars,
        "明日触发条件": trig,
        "参考关注区间": zone,
        "风险点": riskline,
        "factors": c.get("factors") or {},   # 供次日验证与优化器分桶（前端不渲染）
    }


def build_pool(date8, bundles, data):
    """C_Final 主板概念口径备选池：环境配额 + 涨停组(≥55) + 非涨停组(≥55) + 负面清单。

    选股范围仅沪深主板；研究层（题材涨停计数）用全市场。板块因子为真实行业指数
    口径（轮动日线 90 行业：活跃度门槛 + sector_mix 连续打分，行业归属走共享映射）；
    轮动数据缺失的历史回填日非涨停组退回龙虎榜原因代理口径并在 note 标明。
    数据边界近似：龙虎榜辨识度今日-only、ST 以名称过滤、上市天数以窗口 bar 数近似。
    打分细节在 _lu_candidate/_nlu_candidate，输出行组装在 _pool_pick_row。
    """
    zt = bundles.get("zt") or []
    hot = bundles.get("hot") or []
    lhb = bundles.get("lhb") or []
    themes = (data.get("themes") or {}).get("groups") or []
    cycle = data.get("cycle") or {}
    sent_rows = read_sentiment_rows()
    try:
        idx5 = index_gain(index_series("SH", date8), _date_iso(date8), 5)
    except Exception:  # noqa: BLE001
        idx5 = None
    us_row = _us_row(date8)
    us_gate = _us_gate_hit(us_row)
    env, cur, prev_lb = _mf_env(sent_rows, date8, us_row)
    qz, qn = _MF_QUOTA[env]
    # C7 市场量能闸门：缩量日不低吸（env 配额之外的市场级开关，缺数据不启用）
    amt_ratio = _market_amt_ratio(date8)
    amt_gate = (amt_ratio is not None and amt_ratio < _NLU_AMT_RATIO_MIN)
    if amt_gate:
        qn = 0
    yizi_prev = _prev_yizi_codes(date8, sent_rows)
    opt = _load_opt()
    ms_lu = float(opt["min_score"]["lu"])
    ms_nlu = float(opt["min_score"]["nlu"])
    pen_lu = opt["penalties"]["lu"]
    pen_nlu = opt["penalties"]["nlu"]

    theme_count = {t["题材"]: t["家数"] for t in themes}
    zt_codes = {str(cget(r, "code") or "") for r in zt}
    lhb_by = {str(cget(r, "code") or ""): r for r in lhb}
    hot_codes = {str(cget(r, "code") or "") for r in hot}

    # 真实板块口径（M_Final）：90 行业当日/5日强度分位 + 板块涨停家数（共享映射归属）
    try:
        ind_stats, ind_meta = _rotation_strength(
            date8, [str(cget(r, "code") or "") for r in zt])
    except Exception:  # noqa: BLE001
        ind_stats, ind_meta = None, None   # None=代理口径（{} 会让 _nlu_candidate 全排除）
    ind_by = _industry_lookup() if ind_stats else {}
    try:
        volr_map = _lu_volr(date8, list(zt_codes))
    except Exception:  # noqa: BLE001
        volr_map = {}

    # 概念维度（2026-09-04）：个股→概念归属映射（7 天新鲜度，缺则诚实降级）
    cmap = concept_map.load()
    try:
        heat = _concept_heat(zt_codes, cmap, bundles.get("concepts") or [])
    except Exception:  # noqa: BLE001
        heat = ({}, {}, {})

    # ---- 涨停组：C_Final 权重打分（负面清单/门槛在 _lu_candidate 内），取前 qz
    zt_cands = [c for c in (_lu_candidate(r, theme_count, yizi_prev, pen_lu, ms_lu,
                                          volr_map, cmap, heat)
                            for r in zt) if c]
    zt_cands.sort(key=lambda x: (-x["score"], x["code"]))
    zt_picks = zt_cands[:qz]

    # ---- 非涨停组：DuckDB 趋势扫描 + 打分，同板块最多 1 只，取前 qn
    nz_picks = []
    if qn > 0:
        try:
            trend = scan_trend(date8)
        except Exception:  # noqa: BLE001
            trend = []
        nz = [c for c in (_nlu_candidate(m, theme_count, zt_codes, lhb_by,
                                         hot_codes, pen_nlu, ms_nlu, ind_stats, ind_by,
                                         cmap, heat)
                          for m in trend) if c]
        nz.sort(key=lambda x: (-x["score"], x["code"]))
        seen = set()
        for c in nz:  # 同板块最多 1 只
            if c["theme"] in seen:
                continue
            seen.add(c["theme"])
            nz_picks.append(c)
            if len(nz_picks) >= qn:
                break

    # 名称补全（非涨停组无快照名称：龙虎榜/热股/偏离值面板 → ht.symbol_names 兜底）
    dev_by = {r.get("代码"): r for r in ((data.get("deviation") or {}).get("rows") or [])}
    name_map = None
    for c in nz_picks:
        if c["name"]:
            continue
        src = lhb_by.get(c["code"]) or dev_by.get(c["code"]) or \
            next((h for h in hot if str(cget(h, "code") or "") == c["code"]), None)
        name = (src or {}).get("名称") or (src or {}).get("name")
        if not name:
            if name_map is None:
                try:
                    name_map = ht.symbol_names(cache_dir=CACHE_DIR)
                except Exception:  # noqa: BLE001
                    name_map = {}
            name = name_map.get(c["code"])
        c["name"] = name or c["code"]

    picks = zt_picks + nz_picks
    out_picks = [_pool_pick_row(c) for c in picks]

    env_cn = _MF_ENV_CN[env]
    _us_ndx_txt = (f"{round(us_row.get('纳斯达克综合'), 2)}%" if us_row
                   and us_row.get("纳斯达克综合") is not None else "-")
    _amt_txt = f"{round(amt_ratio, 2)}" if amt_ratio is not None else "-"
    market = {
        "情绪判断": f"{env_cn}（{cycle.get('phase', '-')}）· 涨停 {int(cur.get('zt') or 0)} 家"
                    f"·上涨占比 {(cur.get('up_ratio') or 0) * 100:.0f}%"
                    f"·跌停 {int(cur.get('dt') or 0)} 家·上证5日 {round(idx5, 2) if idx5 is not None else '-'}%"
                    f"·隔夜纳指 {_us_ndx_txt}·市场量能 {_amt_txt}"
                    + ("·隔夜美股重挫强制防守" if us_gate else "")
                    + ("·缩量闸门今日不低吸" if amt_gate else ""),
        "主线方向": "、".join([t["题材"] for t in themes[:2]]) or "无明显主线",
        "操作策略": f"环境={env_cn}·配额 涨停{qz}+非涨停{qn}"
                    + ("·冰点空仓观察" if env == "freeze" else
                       "·进攻环境只做涨停组" if env == "aggressive" else "·纪律执行买点/风险位")
                    + ("·防守/冰点档建议半仓（资金管理层 S1）" if env in ("defensive", "freeze") else ""),
    }
    risk = {
        "abort": ["涨停组低开(开盘<0)或高开>+5% 放弃；非涨停组开盘超出 -2%~+3% 放弃",
                  "跌停≥30（恐慌）→ 强制防守；冰点（涨停<25 且上涨占比<30%）→ 只观察",
                  "大面积断板+高位补跌+跌停上升 三者叠加 → 空仓观察一日"],
        "avoid": ["主板以外(创业板/科创板/北交所)/ST/次新/昨日一字板/尾盘板(>14:45)/独狼板(题材内涨停<2家)"
                  "/概念独狼(全部概念走弱且概念内无第2家涨停,C_Final)/成交<1.5亿",
                  "非涨停：贴60日高点(>0.95)追高/深跌(<0.86)接飞刀/放量(量比>1.2)/成交<10亿/20日涨幅>30%/趋势空头"
                  "/行业当日分位<0.6(行业明确走弱)/概念温吞(概念内涨停<3家)降权不回避"],
    }
    concl = {
        "策略": market["操作策略"],
        "最优先关注": f"{out_picks[0]['名称']}（{out_picks[0]['代码']}）" if out_picks else "空仓观察",
        "仅观察不急于买入": "、".join([p["名称"] for p in out_picks if p["入选强度"] <= 2]) or "-",
    }
    _sector_txt = (f"板块因子=真实行业指数(轮动日线90行业, 锚定 {ind_meta['as_of']})"
                   if ind_meta else
                   "板块因子=代理口径(轮动数据缺失)")
    return {"ok": True, "phase": cycle.get("phase"), "env": env, "env_cn": env_cn,
            "quota": {"涨停组": qz, "非涨停组": qn},
            "market": market, "picks": out_picks, "risk": risk, "concl": concl,
            "validation": data.get("pool_validation"),
            "optimizer": data.get("pool_optimizer"),
            "sector_meta": ind_meta,
            "note": f"C7 主板概念口径（strategy-iter C1→C3 概念收敛"
                    f"，C4→C6 隔夜美股闸门，C7 门槛70+市场量能闸门：expB/expC 单变量实验采纳）"
                    f"·资金管理层 S1=防守/冰点档半仓（建议层，实盘自选）"
                    f"·每日验证自我优化·规则化模拟验证，非投资建议；"
                    f"选股范围=沪深主板(600/601/603/605/000/001/002/003)，研究层用全市场；"
                    f"概念为主(驱动概念=所属概念中当日涨幅最高者)、行业为辅；"
                    f"{_sector_txt}；龙虎榜今日-only/ST名称/上市bar数/概念归属7天新鲜度为数据边界近似"}


# ---------------------------------------------------------------- 每日验证与自我优化
# 闭环：T-1 日盘后选出 T 日关注标的 → T 日收盘后用真实走势验证（买点/风险位/归因）
# → 滚动窗口统计 → 有界机械调参（因子分桶惩罚 + 入选门槛微调）。
# 纪律（对应《自动选股与策略自迭代系统.md》）：结构规则/环境配额/执行层永不自动改动；
# 优化器状态是验证记录的纯函数（可重放、回填幂等，不做棘轮累积）；全部调整留痕。
# 单日结果不直接改规则——一切调整都来自 ≥45 日窗口的聚合统计，且样本不足时不动作。

def ohlc_rets(date8, codes):
    """T 日相对 T-1 收盘的开/高/低/收涨幅（%）与量比：{code6: {o,h,l,c,amtr}}。"""
    if not codes:
        return {}
    d = _date_iso(date8)
    start = (_dt.datetime.strptime(d, "%Y-%m-%d")
             - _dt.timedelta(days=15)).strftime("%Y-%m-%d")
    in_sql = ", ".join("'" + thscode_of(c) + "'" for c in sorted(set(codes)))
    sql = (
        "WITH w AS (SELECT thscode, date, open, high, low, close, amount, "
        "LAG(close,1) OVER (PARTITION BY thscode ORDER BY date) pc, "
        "LAG(amount,1) OVER (PARTITION BY thscode ORDER BY date) pa "
        f"FROM v_daily_qfq WHERE date >= DATE '{start}' AND thscode IN ({in_sql})) "
        "SELECT thscode, ROUND((open/NULLIF(pc,0)-1)*100,2) o, ROUND((high/NULLIF(pc,0)-1)*100,2) h, "
        "ROUND((low/NULLIF(pc,0)-1)*100,2) l, ROUND((close/NULLIF(pc,0)-1)*100,2) c, "
        "ROUND(amount/NULLIF(pa,0),2) amtr "
        f"FROM w WHERE date = DATE '{d}'"
    )
    rows = _db_export(sql, f"poolval_{date8}")
    return {str(r.get("thscode") or "").split(".")[0]: r for r in rows}


def _val_attribute(grp, o, c, l, amtr, triggered, b, ind_pct=None, con_pct=None):
    """归因（口径对齐 strategy-iter 验证器：板块/概念持续性不足 = 当日板块/概念指数
    ≤-0.5% 且个股未涨；概念维度 C 线新增，排在板块之后、资金承接之前）。"""
    if c is None:
        return "停牌/数据缺失"
    if not triggered and abs(c) < 2.5:
        return "买点未触发"
    if grp == "lu" and o is not None and o >= 3.0 and c <= o - 3.0:
        return "情绪兑现/高开回落"
    if o is not None and o > b["win_max"] and c < 0:
        return "买点过高"
    if ind_pct is not None and ind_pct <= -0.5 and c < 1.0:
        return "板块持续性不足"
    if con_pct is not None and con_pct <= -0.5 and c < 1.0:
        return "概念持续性不足"
    if l is not None and l <= b["risk_low"]:
        return "资金承接不足"
    if amtr is not None and amtr < 0.55 and c < 0:
        return "资金承接不足"
    if abs(c) < 1.5:
        return "随机波动"
    if c >= 1.5:
        return "正常兑现"
    return "个股辨识度不够/其他"


def validate_prev_pool(date8, sent_rows, concept_pct=None):
    """验证上一交易日备选池：昨日选的票今天是否成功（买点/风险位/归因）。

    concept_pct：{概念名: 当日概念指数涨幅%}（T+1 快照 concepts 模块，调用方传入），
    用于 strong_concept（T+1 收盘强于所属概念指数，引擎 C 线同口径）与
    「概念持续性不足」归因；缺失（概念数据断链/旧线 picks 无 concept 因子）时
    置 None——区分「缺失」与「跑输」，与引擎 notna 守卫同纪律。
    返回 payload；无上一交易日或其快照无备选池时 ok=False（仍留痕，让优化器知道空窗）。
    旧 V3 口径的昨日 picks（无 factors）照常验证结果、归因，但不参与优化器分桶学习
    （rules=v3/mf 标记，优化器只学 C_Final 口径样本）。
    """
    iso = _date_iso(date8)
    prevs = [r["date"] for r in sent_rows if (r["date"] or "") < iso]
    if not prevs:
        return None
    prev8 = prevs[-1].replace("-", "")
    try:
        snap = snapio.load(prev8)
    except Exception:  # noqa: BLE001
        snap = None
    mod = ((snap or {}).get("modules") or {}).get("speculation") or {}
    pool = (mod.get("data") or {}).get("pool") or {}
    picks = pool.get("picks") or []
    rules_tag = ("cf" if "C_Final" in str(pool.get("note") or "")
                 else "mf" if "M_Final" in str(pool.get("note") or "") else "v3")
    if not picks:
        return {"ok": False, "date": prev8, "rules": rules_tag, "picks": [],
                "note": "上一交易日快照无备选池标的（冰点或模块缺失）"}
    try:
        ohlc = ohlc_rets(date8, [str(p.get("代码") or "") for p in picks])
    except Exception:  # noqa: BLE001
        ohlc = {}
    try:
        idx1 = index_gain(index_series("SH", date8), iso, 1)
    except Exception:  # noqa: BLE001
        idx1 = None
    ind_map = _industry_lookup()   # {code6: (881代码, 行业名)}；映射/目录缺失时板块对比诚实置空
    out = []
    for p in picks:
        code = str(p.get("代码") or "")
        grp = "nlu" if p.get("类型") == "非涨停板" else "lu"
        b = _MF_BUY[grp]
        r = ohlc.get(code) or {}
        o, h, l, c = r.get("o"), r.get("h"), r.get("l"), r.get("c")
        row = {"code": code, "name": p.get("名称"), "group": grp,
               "pick_score": p.get("得分"), "rules": rules_tag,
               "buckets": _pick_buckets(p.get("factors")),
               "env": pool.get("env")}
        if c is None or o is None:
            row.update({"attribution": "停牌/数据缺失"})
            out.append(row)
            continue
        in_window = bool(b["win_min"] <= o <= b["win_max"])
        if grp == "lu":
            triggered = in_window and l is not None and l >= b["low_min"] and c > o
        else:
            # 低吸触发：开盘在窗内且收盘站上 max(开盘, 昨收)（昨收涨幅=0）
            triggered = in_window and c > max(o, 0.0)
        risk_hit = (l is not None and l <= b["risk_low"]) or (c <= b["risk_close"])
        win = c > 0
        ind = ind_map.get(code)
        ind_pct = None
        if ind:
            try:
                ind_pct = _series_pct(industry_series(ind[0], date8), iso)
            except Exception:  # noqa: BLE001 - 行业序列取不到时板块对比置空
                ind_pct = None
        attr = ("市场系统性风险"
                if (idx1 is not None and idx1 <= -1.0 and c < 0)
                else _val_attribute(grp, o, c, l, r.get("amtr"), triggered, b,
                                    ind_pct,
                                    (concept_pct or {}).get((p.get("factors") or {}).get("concept"))))
        con_pct1 = (concept_pct or {}).get((p.get("factors") or {}).get("concept"))
        row.update({"open": o, "high": h, "low": l, "close": c,
                    "amt_ratio": r.get("amtr"), "in_window": in_window,
                    "buy_triggered": triggered, "risk_hit": risk_hit,
                    "win": win, "strong_index": (idx1 is not None and c > idx1),
                    "industry": ind[1] if ind else None, "ind_pct": ind_pct,
                    "strong_sector": (ind_pct is not None and c > ind_pct),
                    "concept": (p.get("factors") or {}).get("concept"),
                    "con_pct1": con_pct1,
                    # None=概念数据缺失（不判）；True/False=强于/弱于所属概念指数
                    "strong_concept": (c > con_pct1 if (con_pct1 is not None) else None),
                    "attribution": attr})
        out.append(row)
    return {"ok": True, "date": prev8, "env": pool.get("env"), "rules": rules_tag,
            "picks": out, "stats": {"overall": _val_stats(out),
                                    "lu": _val_stats([p for p in out if p["group"] == "lu"]),
                                    "nlu": _val_stats([p for p in out if p["group"] == "nlu"])},
            "note": None}


def _val_stats(rows):
    done = [p for p in rows if p.get("close") is not None]
    if not done:
        return {"n": len(rows), "valid": 0}
    cr = [p["close"] for p in done]

    def pct(f):
        return round(100.0 * sum(1 for p in done if f(p)) / len(done), 1)
    attr = {}
    for p in done:
        attr[p.get("attribution") or "-"] = attr.get(p.get("attribution") or "-", 0) + 1
    sec = [p for p in done if p.get("ind_pct") is not None]
    con = [p for p in done if p.get("strong_concept") is not None]
    return {"n": len(rows), "valid": len(done),
            "win_rate": pct(lambda p: p["close"] > 0),
            "avg_close": round(sum(cr) / len(cr), 2),
            "avg_open": round(sum(p["open"] for p in done) / len(done), 2),
            "buy_trigger_rate": pct(lambda p: p.get("buy_triggered")),
            "risk_hit_rate": pct(lambda p: p.get("risk_hit")),
            "strong_sector_rate": (round(100.0 * sum(1 for p in sec if p.get("strong_sector")) / len(sec), 1)
                                   if sec else None),
            "sector_n": len(sec),
            "strong_concept_rate": (round(100.0 * sum(1 for p in con if p.get("strong_concept")) / len(con), 1)
                                    if con else None),
            "concept_n": len(con),
            "attribution": dict(sorted(attr.items(), key=lambda x: -x[1]))}


def _load_track():
    try:
        with open(POOL_TRACK, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("validations"), dict):
            return d
    except Exception:  # noqa: BLE001 - 无/坏文件=空记录起步
        pass
    return {"validations": {}}


def _save_track(track):
    os.makedirs(os.path.dirname(POOL_TRACK), exist_ok=True)
    Path(POOL_TRACK).write_text(json.dumps(track, ensure_ascii=False),
                                encoding="utf-8")


def record_validation(date8, val):
    """验证记录按验证日覆盖写（回填/重跑幂等）。"""
    track = _load_track()
    track["validations"][str(date8)] = val
    # 只保留最近 180 个验证日，防无限膨胀
    keys = sorted(track["validations"])[-180:]
    track["validations"] = {k: track["validations"][k] for k in keys}
    _save_track(track)


def _optimizer_state(date8):
    """滚动窗口（仅 C_Final 口径样本）→ 有界旋钮。纯函数：同输入必同输出。"""
    track = _load_track()
    iso_day = _dt.datetime.strptime(_date_iso(date8), "%Y-%m-%d")
    cutoff = iso_day - _dt.timedelta(days=_OPT_WINDOW_DAYS)
    rows = []
    for key in sorted(track.get("validations") or {}):
        try:
            kd = _dt.datetime.strptime(key, "%Y%m%d")
        except ValueError:
            continue
        if kd < cutoff or kd > iso_day:
            continue
        for p in ((track["validations"][key] or {}).get("picks") or []):
            # 只学 C_Final 口径样本（旧线分桶语义不同：如 concept_cold 阈值已变）；
            # 旧线 picks 照常验证留痕，与 M 线"只学本线"纪律一致
            if p.get("rules") != "cf" or p.get("close") is None:
                continue
            rows.append(p)
    state = {"min_score": {}, "penalties": {}, "samples": {}, "bucket_stats": {}}
    for grp in ("lu", "nlu"):
        g = [p for p in rows if p.get("group") == grp]
        lo, hi = _OPT_MIN_SCORE[grp]
        cr = [p["close"] for p in g]
        ms = float(lo)
        if len(cr) >= _OPT_MINSCORE_MIN_N and sum(cr) / len(cr) < 0:
            # 近期均值为负 → 抬门槛过滤边缘分（幅度与负程度挂钩，封顶 hi）
            ms = min(hi, lo + min(10.0, math.ceil(abs(sum(cr) / len(cr)) * 2)))
        state["min_score"][grp] = ms
        state["samples"][grp] = len(g)
        buckets = {}
        for p in g:
            for bk in (p.get("buckets") or []):
                buckets.setdefault(bk, []).append(p["close"])
        pens, bstat = {}, {}
        for bk, xs in sorted(buckets.items()):
            avg = sum(xs) / len(xs)
            bstat[bk] = {"n": len(xs), "avg": round(avg, 2)}
            if len(xs) >= _OPT_BUCKET_MIN_N:
                if avg <= -1.0:
                    pens[bk] = 4
                elif avg <= -0.4:
                    pens[bk] = 2
        state["penalties"][grp] = pens
        state["bucket_stats"][grp] = bstat
    return state


# ---- 样本外追踪与到期复审（C_Final 固化后）----
# C_Final 在 2026-01-02→09-03 的 162 个选股日上调参收敛（runs/concept_round3_C3）；
# 此后每日验证闭环里 rules="cf" 的记录就是真正的样本外。观察不能只挂不办：
# 每满 20 个样本外验证日复审一次，胜率/均次跌破明确阈值必须给出结论（写死，不漂移）。
_OOS_BASELINE = {"win_rate": 59.75, "avg_close": 2.128}   # C3 stats.json 整体口径
_OOS_REVIEW_EVERY = 20      # 复审周期（样本外验证日数）
_OOS_WARN_WIN_DROP = 8.0    # 胜率较基线回落 ≥8pct → 警告（轮间稳定阈值 3pct 的两倍余量）
_OOS_WARN_MEAN = 1.0        # 或均次 <1.0%（基线约一半）
_OOS_CRIT_WIN_DROP = 12.0   # 胜率回落 ≥12pct 或均次 <0 → 严重衰减，建议重开迭代

_OOS_VERDICT_TEXT = {
    "ok": "复审通过：样本外与调参区间基线无实质差异，C_Final 继续运行",
    "warn": "警告：样本外表现回落——人工复核归因分布，规则暂不动",
    "severe": "严重衰减：按《自动选股与策略自迭代系统.md》重开完整迭代",
}


def _oos_verdict(win_rate, avg_close, n):
    """累积样本外统计 → 判定（纯函数）。n<=0 返回 empty。"""
    if n <= 0:
        return "empty"
    drop = _OOS_BASELINE["win_rate"] - win_rate
    if drop >= _OOS_CRIT_WIN_DROP or avg_close < 0:
        return "severe"
    if drop >= _OOS_WARN_WIN_DROP or avg_close < _OOS_WARN_MEAN:
        return "warn"
    return "ok"


def _cf_rows(track):
    """全部 C_Final 口径验证样本（close 非空）。样本外追踪与观察项共用。"""
    rows = []
    for key in sorted(track.get("validations") or {}):
        v = (track.get("validations") or {}).get(key) or {}
        if v.get("rules") != "cf":
            continue
        rows.extend(p for p in (v.get("picks") or []) if p.get("close") is not None)
    return rows


def _oos_state(track):
    """pool_track → C_Final 样本外追踪（纯函数）。

    只统计 rules=="cf" 的验证记录：逐日聚合 + 累积序列 + 整体统计与当前判定。
    """
    vals = track.get("validations") or {}
    per_day, rows_all = [], []
    for key in sorted(vals):
        v = vals[key] or {}
        if v.get("rules") != "cf":
            continue
        picks = [p for p in (v.get("picks") or []) if p.get("close") is not None]
        rows_all.extend(picks)
        cr = [p["close"] for p in picks]
        row = {"date": key, "valid": len(picks),
               "win_rate": (round(100.0 * sum(1 for x in cr if x > 0) / len(cr), 1)
                            if cr else None),
               "avg_close": round(sum(cr) / len(cr), 2) if cr else None}
        row["_wins"] = sum(1 for x in cr if x > 0)
        row["_sum"] = sum(cr)
        per_day.append(row)
    cr = [p["close"] for p in rows_all]
    n = len(cr)
    win_rate = round(100.0 * sum(1 for x in cr if x > 0) / n, 2) if n else None
    avg_close = round(sum(cr) / n, 3) if n else None
    trig = [p for p in rows_all if p.get("buy_triggered")]
    sc = [p for p in rows_all if p.get("strong_concept") is not None]
    cum, rn, rs, rw = [], 0, 0.0, 0
    for d in per_day:
        rn += d["valid"]
        rs += d["_sum"]
        rw += d["_wins"]
        cum.append({"date": d["date"], "n": rn,
                    "win_rate": round(100.0 * rw / rn, 2) if rn else None,
                    "avg_close": round(rs / rn, 3) if rn else None})
    for d in per_day:
        d.pop("_wins")
        d.pop("_sum")   # 内部累加字段不下发
    days = len(per_day)
    return {"days": days, "n": n,
            "since": per_day[0]["date"] if per_day else None,
            "win_rate": win_rate, "avg_close": avg_close,
            "trigger_rate": (round(100.0 * len(trig) / n, 1) if n else None),
            "strong_concept_rate": (round(100.0 * sum(1 for p in sc if p.get("strong_concept")) / len(sc), 1)
                                    if sc else None),
            "baseline": dict(_OOS_BASELINE),
            "verdict": _oos_verdict(win_rate or 0.0, avg_close or 0.0, n),
            "review_every": _OOS_REVIEW_EVERY,
            "next_review_in": (_OOS_REVIEW_EVERY - (days % _OOS_REVIEW_EVERY)) if days else None,
            "per_day": per_day[-10:], "cum": cum}


# ---- 观察项到期动作（rounds_log C 线终止决定遗留观察项③④）----
# 观察项不能无限期挂着：累积样本到线必须二选一——升级（给下轮迭代明确建议）或销项。
_OBS_CONCEPT_COLD_N = 30        # 概念温吞桶（concept_cold：概念内涨停<3 家仍入选）到期线
_OBS_CONCEPT_COLD_AVG = -0.4    # 升级阈值（与优化器分桶惩罚同阈：均次 ≤-0.4%）
_OBS_STRONG_CONCEPT_N = 40      # strong_concept 率观察到期线
_OBS_STRONG_CONCEPT_MIN = 30.0  # 强于概念率 <30%（历史 ~41%，调参区间 51.6%）→ 复审概念因子


def _observation_state(rows):
    """C_Final 全量样本 → 两个遗留观察项的到期状态（纯函数）。

    - concept_cold（遗留③「概念内涨停 ≤2 家残余负桶样本不足，继续观察」）：
      到线后均次 ≤-0.4% → escalate（建议下轮迭代把概念家数 <3 从降权升为硬排除）；
      否则 closed（负桶未证实，销项）。
    - strong_concept（遗留④「跑赢自身概念指数难度高，仅作监控不作门槛」）：
      到线后率 <30% → escalate（概念因子有效性存疑，提示复审）；否则 closed
      （确认结论：维持仅监控，不作门槛）。
    """
    cc = [p["close"] for p in rows if "concept_cold" in (p.get("buckets") or [])]
    sc = [p for p in rows if p.get("strong_concept") is not None]
    th_cc = {"n": _OBS_CONCEPT_COLD_N, "avg": _OBS_CONCEPT_COLD_AVG}
    if len(cc) >= _OBS_CONCEPT_COLD_N:
        avg = sum(cc) / len(cc)
        out_cc = {"n": len(cc), "avg_close": round(avg, 2),
                  "status": "escalate" if avg <= _OBS_CONCEPT_COLD_AVG else "closed",
                  "thresholds": th_cc}
    else:
        out_cc = {"n": len(cc), "status": "observing", "thresholds": th_cc}
    th_sc = {"n": _OBS_STRONG_CONCEPT_N, "min_rate": _OBS_STRONG_CONCEPT_MIN}
    if len(sc) >= _OBS_STRONG_CONCEPT_N:
        rate = 100.0 * sum(1 for p in sc if p.get("strong_concept")) / len(sc)
        out_sc = {"n": len(sc), "rate": round(rate, 1),
                  "status": "escalate" if rate < _OBS_STRONG_CONCEPT_MIN else "closed",
                  "thresholds": th_sc}
    else:
        out_sc = {"n": len(sc), "status": "observing", "thresholds": th_sc}
    return {"concept_cold": out_cc, "strong_concept": out_sc}


def update_optimizer(date8):
    """重算优化器状态并留痕；返回随快照下发的说明 payload。"""
    state = _optimizer_state(date8)
    opt = _load_opt()
    prev_log = opt.get("log") or []
    last = prev_log[-1] if prev_log else None
    # 比对基准：上一验证日状态；首次运行与 C_Final 默认态比对（默认口径→首个非默认
    # 状态也算一次调整，否则首个负期望窗口永远不会被记录）
    ref = last if last else {"min_score": {"lu": 55.0, "nlu": 55.0},
                             "penalties": {"lu": {}, "nlu": {}}}
    changed = []
    if (ref.get("min_score") or {}) != state["min_score"]:
        changed.append("入选门槛 {}→{}".format(ref.get("min_score"), state["min_score"]))
    for grp in ("lu", "nlu"):
        a = (ref.get("penalties") or {}).get(grp) or {}
        bb = state["penalties"][grp]
        for k in sorted(set(a) | set(bb)):
            if a.get(k) != bb.get(k):
                changed.append(f"{'涨停组' if grp == 'lu' else '低吸组'}.{k} "
                               f"惩罚 {a.get(k) or 0}→{bb.get(k) or 0}")
    log = [e for e in prev_log if e.get("date") != str(date8)]
    log.append({"date": str(date8), "min_score": state["min_score"],
                "penalties": state["penalties"], "samples": state["samples"],
                "changed": changed})
    log = log[-90:]
    n_adj = len([e for e in log if e.get("changed")])
    ver = f"C_Final+o{n_adj}" if n_adj else "C_Final"
    enough = state["samples"]["lu"] + state["samples"]["nlu"] >= _OPT_MINSCORE_MIN_N
    # 样本外追踪 + 到期复审 + 观察项到期（每日验证闭环的「验收」侧，与旋钮同处留痕）
    track_all = _load_track()
    oos = _oos_state(track_all)
    obs = _observation_state(_cf_rows(track_all))
    reviews = list(opt.get("oos_reviews") or [])
    review = None
    if oos["days"] > 0 and oos["days"] % _OOS_REVIEW_EVERY == 0 \
            and not any(r.get("date") == str(date8) for r in reviews):
        review = {"date": str(date8), "n_days": oos["days"], "n": oos["n"],
                  "win_rate": oos["win_rate"], "avg_close": oos["avg_close"],
                  "verdict": oos["verdict"],
                  "note": _OOS_VERDICT_TEXT.get(oos["verdict"], oos["verdict"])}
        reviews.append(review)
        reviews = reviews[-20:]
    oos["reviews"] = reviews[-5:]
    oos["review_today"] = review
    out = {"version": ver, "updated": str(date8),
           "min_score": state["min_score"], "penalties": state["penalties"],
           "bucket_stats": state["bucket_stats"], "samples": state["samples"],
           "window_days": _OPT_WINDOW_DAYS, "changes": changed,
           "oos": oos, "observations": obs,
           "note": ("今日调整： " + "；".join(changed)) if changed else
                   ("窗口内无需调整（涨停组 n=%d / 低吸组 n=%d）"
                    % (state["samples"]["lu"], state["samples"]["nlu"])) if enough else
                   ("窗口样本积累中（涨停组 n=%d / 低吸组 n=%d），按 C_Final 默认口径"
                    % (state["samples"]["lu"], state["samples"]["nlu"]))}
    os.makedirs(os.path.dirname(POOL_OPT), exist_ok=True)
    Path(POOL_OPT).write_text(
        json.dumps({"version": ver, "updated": str(date8),
                    "min_score": state["min_score"], "penalties": state["penalties"],
                    "oos_reviews": reviews,
                    "log": log}, ensure_ascii=False),
        encoding="utf-8")
    return out


# ---------------------------------------------------------------- 组装

def compute(date8, bundles, prev_bundles=None, sent_rows=None, historical=None):
    """主入口：返回模块结果 {"status": "ok", "data": {...}}。

    bundles      当日 {zt, zb, dt, hot, lhb}（快照 schema 行，可为空列表）
    prev_bundles 前一交易日 {zt}（fate 用；可为 None）
    sent_rows    sentiment.csv 行（None 时自读）
    historical   是否历史日期（决定异动榜单是否可抓；None 时按是否今天判断）
    """
    t0 = time.time()
    if historical is None:
        historical = date8 != time.strftime("%Y%m%d")
    bundles = bundles or {}
    zt = bundles.get("zt") or []
    zb = bundles.get("zb") or []

    data = {
        "version": VERSION,
        "calc": {"date": date8, "historical": bool(historical),
                 "elapsed_ms": None,
                 "口径": "偏离值=个股区间涨幅−指数涨幅；沪市基准上证指数，深市基准深证成指；"
                         "普通异动 3 日偏离 ±20%(10cm)/±30%(20cm)；"
                         "严重异动 10 日≥100% 或 30 日≥200%；北交所不在判定口径内"},
        "themes": build_themes(zt),
        "ladder": build_ladder(zt),
    }

    try:
        data["deviation"] = {"ok": True, **build_deviation(date8, zt, zb)}
    except Exception as e:  # noqa: BLE001 - 偏离值失败不连坐其他部分
        data["deviation"] = {"ok": False,
                             "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        data["fate"] = build_fate(date8, (prev_bundles or {}).get("zt") or [],
                                  bundles)
    except Exception as e:  # noqa: BLE001
        data["fate"] = {"rows": [], "stats": [],
                        "note": f"计算失败: {type(e).__name__}: {str(e)[:150]}"}

    try:
        data["cycle"] = build_cycle(
            date8, sent_rows if sent_rows is not None else read_sentiment_rows())
    except Exception as e:  # noqa: BLE001
        data["cycle"] = {"phase": "计算失败",
                         "reasons": [f"{type(e).__name__}: {str(e)[:150]}"],
                         "series": [], "indicators": {}}

    try:
        data["anomalies"] = build_anomalies(historical)
    except Exception as e:  # noqa: BLE001
        data["anomalies"] = {"available": False,
                             "note": f"抓取失败: {type(e).__name__}: {str(e)[:150]}"}

    try:
        # 每日验证与自我优化闭环：先验证昨日备选池（真实走势/买点/归因），
        # 再带着最新优化器状态（滚动窗口统计）选今日标的
        _sr = sent_rows if sent_rows is not None else read_sentiment_rows()
        # T+1 概念指数当日涨幅（strong_concept / 概念持续性不足归因用；
        # compute 收到的 bundles 即 T+1 当日快照，历史回填日由 bundles_from_snap 提供）
        _con_pct = {r.get("概念"): r.get("涨跌幅")
                    for r in (bundles.get("concepts") or []) if r.get("概念")}
        val = validate_prev_pool(date8, _sr, concept_pct=_con_pct)
        if val:
            data["pool_validation"] = val
            record_validation(date8, val)
    except Exception as e:  # noqa: BLE001 - 验证失败不连坐选股
        data["pool_validation"] = {"ok": False,
                                   "error": f"{type(e).__name__}: {str(e)[:150]}"}
    try:
        data["pool_optimizer"] = update_optimizer(date8)
    except Exception as e:  # noqa: BLE001 - 优化器失败按默认口径选股
        data["pool_optimizer"] = {"version": "C_Final",
                                  "error": f"{type(e).__name__}: {str(e)[:150]}"}

    try:
        data["pool"] = build_pool(date8, bundles, data)
    except Exception as e:  # noqa: BLE001 - 备选池失败不连坐
        data["pool"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}",
                        "market": None, "picks": [], "risk": None, "concl": None}

    data["rules"] = RULES
    data["calc"]["elapsed_ms"] = int((time.time() - t0) * 1000)
    return {"status": "ok", "data": data}


# ---------------------------------------------------------------- 回填 CLI

def bundles_from_snap(snap):
    mod = (snap or {}).get("modules") or {}

    def rows(name):
        m = mod.get(name) or {}
        return m.get("data") or [] if m.get("status") == "ok" else []

    return {"zt": rows("limit_up_pool"), "zb": rows("limit_break_pool"),
            "dt": rows("limit_down_pool"), "hot": rows("hot_stock"),
            "lhb": rows("lhb"), "concepts": rows("concepts")}


def _save_same_format(snap, date8):
    """写回快照：原来什么格式还什么格式（老快照是 .json.gz）。"""
    p = snapio.snap_path(snapio.RECAP_DATA, date8)
    if p.endswith(".gz"):
        tmp = p + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, p)
        return p
    return snapio.save(date8, snap)


def retrofill(date8, quiet=False):
    """计算单日投机分析并合并进既有快照。返回 (是否计算, 说明)。"""
    snap = snapio.load(date8)
    if snap is None:
        return False, "快照不存在"
    dates = snapio.list_dates()
    prev_dates = [d for d in dates if d < date8]
    prev_bundles = None
    if prev_dates:
        prev_bundles = bundles_from_snap(snapio.load(prev_dates[0]))
    res = compute(date8, bundles_from_snap(snap), prev_bundles)
    # 回填不降级：旧快照里已有当日抓到的异动榜单时保留（anomaly-list 是 today-only
    # 接口，隔天重算只会得到「不可回补」，不能把已有真数据冲掉）
    old_an = (((snap.get("modules") or {}).get("speculation") or {}).get("data") or {}) \
        .get("anomalies") or {}
    if old_an.get("available") and not (res["data"].get("anomalies") or {}).get("available"):
        res["data"]["anomalies"] = old_an
    snap.setdefault("modules", {})["speculation"] = res
    out = _save_same_format(snap, date8)
    if not quiet:
        dev = res["data"].get("deviation") or {}
        n_dev = len(dev.get("rows") or []) if dev.get("ok") else "err"
        cyc = (res["data"].get("cycle") or {}).get("phase")
        n_fate = len((res["data"].get("fate") or {}).get("rows") or [])
        n_theme = len((res['data']['themes'] or {}).get('groups') or [])
        print(f"  {date8}: 题材 {n_theme} 组 · "
              f"偏离候选 {n_dev} · 承接 {n_fate} · 周期「{cyc}」 -> {os.path.basename(out)}")
    return True, "ok"


def main():
    ap = argparse.ArgumentParser(description="投机分析回填（合并进既有快照）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="8 位日期，如 20260902")
    g.add_argument("--recent", type=int, help="最近 N 份快照")
    g.add_argument("--missing", action="store_true", help="所有缺 speculation 的快照")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(HERE))   # backend/（lockutil）
    import lockutil
    lock = lockutil.acquire(os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                           ".status", "fetch-recap.lock"))
    if lock is None:
        print("已有复盘抓取在运行（锁），本次跳过。")
        sys.exit(0)

    dates = snapio.list_dates()   # 倒序
    try:
        if args.date:
            targets = [args.date]
        elif args.recent:
            targets = dates[:args.recent]
        elif args.missing:
            targets = []
            for d in dates:
                s = snapio.load(d)
                if s and "speculation" not in (s.get("modules") or {}):
                    targets.append(d)
        else:
            ap.error("需要 --date / --recent / --missing 之一")
        print(f"== 投机分析回填 {len(targets)} 份快照 ==")
        ok = 0
        for d in targets:
            try:
                done, msg = retrofill(d)
                ok += 1 if done else 0
                if not done:
                    print(f"  {d}: 跳过（{msg}）")
            except Exception as e:  # noqa: BLE001 - 单日失败不中断批量
                print(f"  {d}: 失败 {type(e).__name__}: {str(e)[:150]}")
        print(f"== 完成 {ok}/{len(targets)} ==")
    finally:
        lockutil.release(lock)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
