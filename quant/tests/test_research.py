"""网格搜索与 Walk-Forward 框架测试（小型合成数据，秒级）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_sim.core.config import BacktestConfig
from quant_sim.data.sample import make_synthetic_panel
from quant_sim.research import grid_search, make_folds, param_grid, walk_forward
from quant_sim.strategies import DualMAStrategy


@pytest.fixture(scope="module")
def panel():
    return make_synthetic_panel(n_symbols=2, start="2021-01-04", end="2024-12-31", seed=21, symbols=["510300", "600000"])


def test_param_grid():
    combos = param_grid(a=[1, 2], b=["x", "y"])
    assert len(combos) == 4
    assert {"a": 1, "b": "x"} in combos


def test_make_folds_no_overlap():
    dates = pd.bdate_range("2021-01-04", periods=500)
    folds = make_folds(dates, train_days=244, test_days=63)
    assert len(folds) >= 3
    for f in folds:
        assert f["test_start"] > f["train_end"]
        assert (f["test_end"] - f["test_start"]).days >= 30
    # 相邻折测试窗不重叠（step=test）
    for a, b in zip(folds, folds[1:]):
        assert b["test_start"] > a["test_end"]


def test_make_folds_rejects_overlapping_step():
    dates = pd.bdate_range("2021-01-04", periods=500)
    with pytest.raises(ValueError, match="测试窗重叠"):
        make_folds(dates, train_days=100, test_days=63, step_days=20)


def test_walk_forward_warmup_no_flat_head(panel):
    """预热后折首段不再被强制空仓拍平：首日 OOS 收益可为非零（长回看参数不被饿死）。"""
    cfg = BacktestConfig(initial_cash=200_000)
    res = walk_forward(
        lambda **kw: DualMAStrategy("510300", **kw),
        {"fast": [10], "slow": [60]},
        panel,
        cfg,
        train_days=250,
        test_days=120,
        warmup_days=80,
    )
    assert res.warmup_days == 80
    eq = res.oos_equity
    assert ((eq.pct_change().dropna()) != 0).sum() > 5  # 拼接曲线真实在动，不是头部一排 0


def test_grid_search_orders_and_records_errors(panel):
    grid = {"fast": [10, 60], "slow": [20, 30]}  # fast=60>slow 会触发断言→error 行
    cfg = BacktestConfig(start_date="2021-06-01", end_date="2024-12-31", initial_cash=200_000)
    df = grid_search(lambda **kw: DualMAStrategy("510300", **kw), grid, panel, cfg, rank_by="夏普比率")
    assert len(df) == 4
    assert (df["_error"] != "").sum() >= 2  # 60/20 与 60/30
    ok = df[df["_error"] == ""]
    assert list(ok["夏普比率"]) == sorted(ok["夏普比率"], reverse=True)
    assert ok["排名"].tolist() == list(range(1, len(ok) + 1))


def test_walk_forward_stitches_oos(panel):
    cfg = BacktestConfig(initial_cash=200_000)
    res = walk_forward(
        lambda **kw: DualMAStrategy("510300", **kw),
        {"fast": [5, 10], "slow": [20, 40]},
        panel,
        cfg,
        train_days=250,
        test_days=120,
        rank_by="夏普比率",
    )
    assert len(res.folds) >= 2
    # OOS 拼接长度 = 各折测试窗交易日数之和（读结构化日期列，不解析展示字符串）
    expected = 0
    for rec in res.folds.to_dict("records"):
        s, e = pd.Timestamp(rec["测试窗起"]), pd.Timestamp(rec["测试窗止"])
        expected += int(((panel.dates >= s) & (panel.dates <= e)).sum())
    assert len(res.oos_equity) == expected
    assert res.oos_equity.index.is_monotonic_increasing
    assert res.oos_equity.iloc[0] > 0
    assert "累计收益率" in res.oos_metrics
    assert all(set(["fast", "slow"]) <= set(p) for p in res.best_params_per_fold)
    assert "Walk-Forward" in res.summary()
    # 结构化诊断字段（裁决不嵌在文案里）
    assert res.warmup_days >= 50  # 网格最大数值参数 slow=40 → 自动预热 50
    assert {"level", "value", "threshold", "reason"} <= set(res.verdict)
    assert "衰减率" in res.folds.columns
    assert res.param_stability and set(res.param_stability[0]) >= {"折间", "改动数", "参数数"}
