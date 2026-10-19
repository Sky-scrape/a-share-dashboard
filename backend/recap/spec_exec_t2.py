# -*- coding: utf-8 -*-
"""制度口径（A 股 T+1）可执行收益：单一来源实现（2026-10-16，C15 复审「修测量」）。

动机
----
`spec_validate._exec_pnl_realtime`（与研究侧 `backtest_capital.trade_pnl` 同一实现）
允许 **T+1 买入当日** 触发 risk_low 即止损离场——A 股 T+1 制度下当日买入不可卖出，
该止损**实际不可执行**。C15 复审实测：全窗口诊断口径 vs 制度口径差 −0.03pp（不显著），
但样本外差 +1.54pp；真正吃掉收益的是**强制过夜持有**（T+1 收盘 → T+2 开盘）。
因此本模块提供一套**可执行**口径，与诊断口径并列展示：

- 诊断口径（保留，明确标注）：T+1 盘中触 risk_low 即离场——乐观代理，不可执行。
- 制度口径（本模块）：T+1 按实时买点成交 → T+1 收盘持有（不可卖）→ T+2 开盘卖出。

判定纪律：本模块产物**只作诊断与展示**，不参与 `_oos_verdict` 的 warn/severe 判定
（判定阈值是冻结口径，见 spec_validate 模块头）；换口径要另起研究轮次。

数据来源：沿用 `spec_duckdb.ohlc_rets` 单一来源（本地库 + 腾讯备源兜底）。
T+2 不需要新日期源：`ohlc_rets(T2)` 给的是 T+2 相对 **T+1 收盘** 的涨幅，
与当日记录里的 T+1 收盘（相对 T 收盘）链式相乘即得相对建仓基准的涨幅。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from execution_layer import MF_BUY, FEE_MAIN  # noqa: E402  买点窗/风险位/费用单一来源

FEE_PCT = FEE_MAIN       # 双边费用（%），2026-10-19 P1-5 收编至 execution_layer（别名保留）
SELL_BLOCK_OPEN = -9.8  # T+2 开盘跌幅 ≤ 此值视为跌停封死（10cm）→ 开盘卖不出，改按收盘
RULES_LABEL = "制度口径（T+1 买 → T+2 开盘卖，含费 %.2f%%）" % FEE_PCT

# 跌停开盘的离场价建模选择（2026-10-16 记，供引用数字时对照）：
# 选「等收盘卖」（SELL_BLOCK_OPEN 触发时用 T+2 收盘价）。实测样本外 14 笔中仅 1 笔
# 命中（600802：T+2 开盘 −10.03%、盘中拉回平盘、收 −4.67%），该笔按收盘 −4.67% 计。
# 敏感性：若改为「按开盘价卖出」（即假设跌停价也能立即成交），同一样本整体净均
# 由 −1.89% 变 −2.31%/笔——**两种假设下结论一致（明显为负）**，故取更贴近实操的一种。


# ---------------------------------------------------------------- 纯函数

def entry_of(group, o, h, c_prev=0.0):
    """实时买点判定（与 MF_BUY 窗口一致）：返回 (entry_ret|None, 说明)。

    o/h：T+1 的开盘/最高涨幅（%，相对 T 收盘）；c_prev 固定为 0（同基准）。
    """
    if group not in MF_BUY or o is None:
        return None, "无开盘价"
    b = MF_BUY[group]
    if not (b["win_min"] <= o <= b["win_max"]):
        return None, "开盘超窗放弃"
    if group == "nlu":
        if o >= c_prev:
            return o, "开盘即成交"
        return (c_prev, "回踩昨收成交") if (h is not None and h >= c_prev) else (None, "限价未触及")
    return o, "开盘市价成交"


def _ratio(exit_ret, entry_ret):
    """（昨收相对）涨幅口径 → 相对建仓价的收益（%）；与诊断口径同算法。"""
    return ((1 + exit_ret / 100.0) / (1 + entry_ret / 100.0) - 1) * 100.0


def chain(prev_ret, cur_ret):
    """两段涨幅链式相乘：prev 相对基准、cur 相对 prev，返回相对基准的涨幅（%）。"""
    if prev_ret is None or cur_ret is None:
        return None
    return ((1 + prev_ret / 100.0) * (1 + cur_ret / 100.0) - 1) * 100.0


def t2_net(group, r1, r2, fee=FEE_PCT):
    """制度口径净收益（%）：不成交/数据不足返回 (None, 原因)。

    r1：T+1 行 {o,h,l,c}（相对 T 收盘）；r2：T+2 行 {o,c}（相对 T+1 收盘）。
    返回 (net|None, 说明)。risk_low_hit 只作**风险暴露**留痕——T+1 当日不可卖，
    故不参与离场价，这正是与诊断口径的差别所在。
    """
    if not r1 or not r2:
        return None, "数据不足"
    e_ret, why = entry_of(group, r1.get("o"), r1.get("h"))
    if e_ret is None:
        return None, why
    t2_open_rel = chain(r1.get("c"), r2.get("o"))      # T+2 开盘 相对 T 收盘
    if t2_open_rel is None:
        return None, "T+2 开盘缺失"
    unsellable = t2_open_rel - (r1.get("c") or 0.0) <= SELL_BLOCK_OPEN
    exit_rel = chain(r1.get("c"), r2.get("c")) if unsellable else t2_open_rel
    if exit_rel is None:
        return None, "T+2 收盘缺失"
    return round(_ratio(exit_rel, e_ret) - fee, 3), (
        "T+2 跌停开盘改收盘离场" if unsellable else "T+2 开盘离场")


def exec_t2_stats(rows):
    """按组聚合制度口径：{group: {n, n_exec, pnl_avg, win_rate, risk_exposed}}。"""
    out = {}
    for grp in ("lu", "nlu"):
        rs = [r for r in rows if r.get("group") == grp and "exec_t2" in r]
        vals = [r["exec_t2"]["net"] for r in rs
                if (r.get("exec_t2") or {}).get("net") is not None]
        out[grp] = {
            "n": len(rs),
            "n_exec": len(vals),
            "pnl_avg": round(sum(vals) / len(vals), 3) if vals else None,
            "win_rate": (round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1)
                         if vals else None),
            "risk_exposed": sum(1 for r in rs if (r.get("exec_t2") or {}).get("risk_low_hit")),
        }
    allv = [r["exec_t2"]["net"] for r in rows if "exec_t2" in r
            and (r.get("exec_t2") or {}).get("net") is not None]
    out["overall"] = {
        "n": sum(1 for r in rows if "exec_t2" in r),
        "n_exec": len(allv),
        "pnl_avg": round(sum(allv) / len(allv), 3) if allv else None,
        "win_rate": (round(100.0 * sum(1 for v in allv if v > 0) / len(allv), 1)
                     if allv else None),
    }
    return out


# ---------------------------------------------------------------- 数据适配

def t2_rets(date8, codes):
    """T+2 行：{code: {o,c}}（相对 T+1 收盘涨幅%）。缺行如实缺席。"""
    from spec_duckdb import ohlc_rets  # noqa: PLC0415  单一数据来源
    rows = ohlc_rets(date8, codes) or {}
    return {c: {"o": v.get("o"), "c": v.get("c")} for c, v in rows.items()}


def fill_record(rec, t1, t2):
    """给一条验证记录补 `exec_t2`（就地改 picks；返回补算成功的票数）。

    t1 = 本记录的验证日（= T+1）；t2 = 次一交易日（= T+2，即下一个验证日键）。
    数据缺失时该项 `net=None` 并留 `note`，**不虚构**；幂等（已补的票不重算）。
    """
    picks = [p for p in (rec.get("picks") or []) if p.get("code")]
    if not picks or not (t1 and t2):
        return 0
    pending = [p for p in picks if "exec_t2" not in p]
    if not pending:
        return 0
    r2 = t2_rets(t2, [p["code"] for p in pending])
    filled = 0
    for p in pending:
        r1 = {"o": p.get("open"), "h": p.get("high"), "l": p.get("low"), "c": p.get("close")}
        net, why = t2_net(p.get("group"), r1, r2.get(p["code"]))
        if net is None:
            p["exec_t2"] = {"net": None, "note": why}
            continue
        b = MF_BUY.get(p.get("group")) or {}
        p["exec_t2"] = {
            "net": net,
            "note": why,
            "risk_low_hit": bool(p.get("low") is not None
                                 and p["low"] <= b.get("risk_low", -99)),
            "fee": FEE_PCT,
        }
        filled += 1
    return filled


def fill_recent(track, limit=25):
    """给最近 limit 条验证记录补算制度口径（T+2 已到者）。

    只补近端：`_oos_state` 复审窗口与看板滚动展示都只看近 20 个验证日，
    更早的记录没有诊断价值，避免首次运行一次性打 100+ 次行情查询。
    返回补算的票数；无待补记录时为 0（每日链常态）。
    """
    vals = track.get("validations") or {}
    keys = sorted(vals)
    filled = 0
    for i, k in enumerate(keys):
        if i + 1 >= len(keys):
            break                              # 最后一个验证日无 T+2 → 跳过
        if i < len(keys) - limit:
            continue                           # 只补近端
        rec = vals[k] or {}
        if not (rec.get("picks") or []):
            continue
        filled += fill_record(rec, t1=str(k), t2=str(keys[i + 1]))
    return filled


def rolling_summary(validations, days=20, rules="c7"):
    """近 days 个验证日的制度口径汇总（看板诊断行用；不含当日无 T+2 的记录）。

    只统计指定口径（默认 c7，与样本外追踪同口径）。返回 None 表示窗口内
    没有任何已补算记录（尚未跨过 T+2 或数据缺失）——调用方据此不展示该行。
    """
    keys = [k for k in sorted(validations)
            if (validations[k] or {}).get("rules") == rules][-days:]
    rows = [p for k in keys for p in (validations[k].get("picks") or [])
            if isinstance(p.get("exec_t2"), dict)]
    if not rows:
        return None
    s = exec_t2_stats(rows)
    if not s["overall"]["n"]:
        return None
    return {"days": len({k for k in keys if any(
        isinstance(p.get("exec_t2"), dict) for p in (validations[k].get("picks") or []))}),
        "date_from": keys[0] if keys else None, "date_to": keys[-1] if keys else None,
        "label": RULES_LABEL, "fee": FEE_PCT, **s}
