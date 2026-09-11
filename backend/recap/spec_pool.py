# -*- coding: utf-8 -*-
"""明日交易备选池：环境分档/四档配额 + 涨停组/低吸组打分 + 概念维度 + build_pool 组装与口径版本守卫。"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

import ht  # noqa: E402
import concept_map  # noqa: E402  个股→概念归属（备选池概念维度单一来源）
import us_market  # noqa: E402  隔夜美股因子单一来源（backend/us_market.py）
from modules import cget  # noqa: E402
from spec_duckdb import (CACHE_DIR, _lu_volr, _market_amt_ratio,  # noqa: E402
                         _prev_yizi_codes, scan_trend)
from spec_rules import _date_iso, _first_theme  # noqa: E402
from spec_series import (_industry_catalog, _industry_lookup, index_gain,  # noqa: E402
                         index_series, read_sentiment_rows)
from spec_validate import _load_opt, _pick_buckets  # noqa: E402  优化器状态与共用分桶

HERE = os.path.dirname(os.path.abspath(__file__))

# 备选池口径版本（retrofill 守卫的当前值）：改池口径时必须同步更新这里——
# 旧快照 pool.note 首段记的就是历史口径版本（"C_Final 主板概念口径…"、
# "M_Final 主板口径…"），与本值不一致的历史快照拒绝重算（见 retrofill）。
RULES_VERSION = "C7"


def pool_rules_tag(pool):
    """从备选池 payload 提取口径版本标记；查证结论（2026-09-10，实扫全部 170 份
    快照）：版本记在 pool.note 文案首段——"C7 主板概念口径（…）"→"C7"、
    "C_Final 主板概念口径（…）"→"C_Final"、"M_Final 主板口径（…）"→"M_Final"、
    "V3 最终筛选方案…"→"V3"、"规则化筛选·…"→"规则化筛选"。优化器版本
    pool_optimizer.version（如 "C_Final+o1"）带调整计数、且 pending 池整段缺失，
    不宜作标记。pending 占位池（待隔夜确认，设计上由 T+1 收盘链终版覆盖，无口径
    版本可言）与 note 缺失/无 pool 一律返回 None（有 pool 却无标记按旧口径守卫）。"""
    pool = pool or {}
    if pool.get("stage") == "pending":
        return None
    note = str(pool.get("note") or "").strip()
    if not note:
        return None
    for sep in (" ", "（", "·"):
        note = note.split(sep)[0]
    return note or None


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


_ROT_DAILY = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                          "data", "rotation", "daily")

# C7 非涨停组市场量能闸门（expC 实验：缩量日 NLU -0.24% n=47 / 放量日 +2.13% n=41）
_NLU_AMT_RATIO_MIN = 0.9


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
        # 仅观察=非5星票（C7 全窗口 4星档：胜率60.2%/均收+1.99% vs 5星 63.1%/+2.72%，
        # 明确次档）；首票已列最优先关注，不重复列出
        "仅观察不急于买入": "、".join([p["名称"] for p in out_picks[1:]
                                      if p["入选强度"] <= 4]) or "-",
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
