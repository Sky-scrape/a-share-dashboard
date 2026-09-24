# -*- coding: utf-8 -*-
"""执行层止损口径 parity 测试（2026-09-25 审查批次，方案 §5.2）。

背景：spec_validate._exec_pnl_realtime 曾把 execution_layer 的 low_min(-3%，确认式
买点触发过滤) 误当 LU 止损，与 backtest_capital.trade_pnl 的 risk_low(-5%) 口径错位
（2026-09-24 修正）。本文件把这个口径钉进回归——再出现「同一事实两处实现」漂移时
CI 直接红。

层次：
- 黄金样本：603186 @ 20260923（报告口径，手算可验）；
- 边界三例：low == -5 恰命中（<= 语义）、low ∈ (-5,-3) 新旧必分歧、low == -2 双方一致；
- 公式等价扫描：spec_validate ↔ backtest_capital.trade_pnl(realtime) 全网格逐点相等；
- 三方常量同源：spec_validate / backtest_capital / execution_layer；
- S1 全窗复核：runs/c12_window_C7/validation.csv 重算（文件在 git 内；缺失则跳过）。

全部离线纯计算，无网络无数据依赖（S1 除外，skipif 保护）。
"""
import csv
import os
import sys

import pytest

import execution_layer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "strategy-iter", "scripts"))
import backtest_capital  # noqa: E402

import spec_validate  # noqa: E402

VALIDATION_CSV = os.path.join(ROOT, "strategy-iter", "runs", "c12_window_C7",
                              "validation.csv")


def _old_impl(grp, o, h, l, c):
    """修正前实现的忠实副本（git 2026-09-24 前）：仅作对照基准，不得回归到它。"""
    b = execution_layer.MF_BUY[grp]
    if o is None or c is None or not (b["win_min"] <= o <= b["win_max"]):
        return None
    if grp == "nlu" and (h is None or h < max(o, 0.0)):
        return None
    entry = o if grp == "lu" else max(o, 0.0)
    stop = b["low_min"] if grp == "lu" else b["risk_low"]
    hit = l is not None and ((l < stop) if grp == "lu" else (l <= stop))
    exit_pct = stop if hit else c
    return round(((1 + exit_pct / 100.0) / (1 + entry / 100.0) - 1) * 100, 2)


def _trade_pct(grp, o, h, l, c):
    """backtest_capital 主口径（realtime）换算为 % 两位小数；不成交 None。"""
    pnl = backtest_capital.trade_pnl({"group": grp, "o": o, "h": h, "l": l, "c": c},
                                     mode="realtime")
    return None if pnl is None else round(pnl * 100, 2)


# ---------------------------------------------------------------- 黄金样本与边界

def test_golden_603186_20260923():
    """报告黄金样本：NLU 落窗、risk_hit（low -6.82 ≤ -4）、exec -4.38%。"""
    o, h, l, c = 0.4, 1.45, -6.82, -6.32
    got = spec_validate._exec_pnl_realtime("nlu", o, h, l, c)
    assert got == -4.38
    assert got == _trade_pct("nlu", o, h, l, c)


def test_boundary_low_exactly_at_risk_low_hits():
    """LU low == risk_low(-5)：`<=` 语义必须命中（若有人改回 `<` 此例变 +1.98）。"""
    assert spec_validate._exec_pnl_realtime("lu", 1.0, 2.0, -5.0, 3.0) == -5.94


def test_boundary_old_vs_new_diverge_in_gap():
    """low ∈ (-5,-3)（如 -4）：旧口径误止损于 -3，新口径不止损走 close——分歧样本固化。"""
    o, h, l, c = 1.0, 2.0, -4.0, 3.0
    new = spec_validate._exec_pnl_realtime("lu", o, h, l, c)
    assert new == _trade_pct("lu", o, h, l, c)      # 新口径与主口径一致
    assert new != _old_impl("lu", o, h, l, c)        # 与旧实现确有分歧（修复生效的证据）


def test_boundary_above_stop_both_agree():
    """low 未及风险位：新旧一致走 close。"""
    assert spec_validate._exec_pnl_realtime("lu", 1.0, 2.0, -2.0, 3.0) == \
        _old_impl("lu", 1.0, 2.0, -2.0, 3.0) == \
        _trade_pct("lu", 1.0, 2.0, -2.0, 3.0)


def test_nlu_no_fill_when_high_below_limit():
    """NLU 限价挂 max(开盘,昨收)：当日未触及 = 不成交，双方一致返回 None。"""
    for impl in (spec_validate._exec_pnl_realtime, _trade_pct):
        assert impl("nlu", 2.0, 1.0, -1.0, 2.5) is None


# ---------------------------------------------------------------- 公式等价扫描

@pytest.mark.parametrize("grp", ["lu", "nlu"])
def test_sweep_formula_equivalence(grp):
    """全网格逐点：spec_validate ↔ trade_pnl(realtime)。任何一处漂移即红。"""
    opens = [-3.0, -2.0, 0.0, 0.5, 2.0, 4.0, 6.0]
    highs = [None, -1.0, 0.5, 3.0]
    lows = [None, -6.5, -5.0, -4.0, -3.0, -2.0, 0.0]
    closes = [-7.0, -1.0, 0.5, 2.0]
    for o in opens:
        for h in highs:
            for l in lows:
                for c in closes:
                    assert spec_validate._exec_pnl_realtime(grp, o, h, l, c) == \
                        _trade_pct(grp, o, h, l, c), (grp, o, h, l, c)


def test_three_way_constants_single_source():
    """三方常量同一来源：spec_validate 直接引用；backtest_capital 拷贝自同源。"""
    assert spec_validate._MF_BUY is execution_layer.MF_BUY
    assert backtest_capital.LU == execution_layer.MF_BUY["lu"]
    assert backtest_capital.NLU == execution_layer.MF_BUY["nlu"]
    assert execution_layer.MF_BUY["lu"]["risk_low"] == -5.0
    assert execution_layer.MF_BUY["nlu"]["risk_low"] == -4.0
    assert execution_layer.MF_BUY["lu"]["low_min"] == -3.0   # 触发过滤，不是止损


# ---------------------------------------------------------------- S1 全窗复核

def test_s1_full_window_parity_vs_capital():
    """validation.csv 全窗重算：新实现与 backtest_capital 逐笔零不一致（报告 n=447）。"""
    if not os.path.exists(VALIDATION_CSV):
        pytest.skip("validation.csv 不在本机（CI 环境正常）")
    n_rows = n_both_none = n_exec = n_mismatch = n_single = 0
    gap = below = other = 0
    with open(VALIDATION_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            n_rows += 1
            grp = row["group"]
            try:
                o, h, l, c = (float(row[k]) if row[k] != "" else None
                              for k in ("open_ret", "high_ret", "low_ret", "close_ret"))
            except (KeyError, ValueError):
                continue
            a = spec_validate._exec_pnl_realtime(grp, o, h, l, c)
            b = _trade_pct(grp, o, h, l, c)
            if a != _old_impl(grp, o, h, l, c):
                # 与旧实现分歧的两类，都只能来自本次止损口径修正：
                #  gap   = low ∈ (-5,-3] 的 LU（旧误止损于 -3，新不止损）——报告记 23 笔；
                #  below = low ≤ -5 的 LU（双方都止损，出场 -3 → -5）
                if grp == "lu" and l is not None and -5.0 < l <= -3.0:
                    gap += 1
                elif grp == "lu" and l is not None and l <= -5.0:
                    below += 1
                else:
                    other += 1
            if a is None and b is None:
                n_both_none += 1
            elif (a is None) != (b is None):
                n_single += 1
            else:
                n_exec += 1
                if a != b:
                    n_mismatch += 1
    assert n_rows == 447, f"样本数变化？n={n_rows}（报告基线 447）"
    assert n_single == 0, "存在单边执行——两实现成交判定分歧"
    assert n_mismatch == 0, f"数值不一致 {n_mismatch} 笔"
    assert n_both_none + n_exec == 447
    assert (gap, below, other) == (23, 24, 0), \
        f"分歧分布漂移：gap={gap} below={below} other={other}——若非口径修正所致须人工排查"
