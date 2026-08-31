"""数据层与指标层测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_sim.data.sample import make_demo_panel, make_synthetic_panel
from quant_sim.metrics.performance import (
    annualized_return,
    compute_metrics,
    daily_returns,
    drawdown_series,
    max_drawdown,
    sharpe_ratio,
)
from tests.helpers import flat_rows, make_panel


class TestPanelIntegrity:
    def test_synthetic_respects_price_limits(self):
        panel = make_synthetic_panel(n_symbols=4, start="2022-01-03", end="2024-12-31", seed=11)
        close, pc = panel.arrays["close"], panel.arrays["pre_close"]
        with np.errstate(all="ignore"):
            ret = close / pc - 1
        # 允许 0.5% 舍入宽容（10% 幅度按 0.01 元舍入）
        assert np.nanmax(ret) <= 0.105
        assert np.nanmin(ret) >= -0.105

    def test_halt_rows_excluded(self):
        rows = flat_rows(10, 10.0)
        rows[4] = {"open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan, "volume": 0}
        panel = make_panel({"600000": rows})
        hist = panel.history("600000", panel.dates[-1])
        assert len(hist) == 9
        assert panel.dates[4] not in hist.index

    def test_history_never_future(self):
        panel = make_demo_panel(seed=3)
        s = panel.symbols[0]
        mid = panel.dates[len(panel.dates) // 2]
        h = panel.history(s, mid, 30)
        assert h.index[-1] <= mid
        assert len(h) <= 30

    def test_roundtrip_long(self):
        panel = make_synthetic_panel(n_symbols=2, start="2023-01-02", end="2023-06-30", seed=5, symbols=["600000", "000001"])
        long = panel.to_long()
        again = type(panel).from_long(long)
        # 长表会丢弃“全标的均无行情”的空日期行，日期轴应变短（停牌日剔除）
        assert again.n_dates <= panel.n_dates
        assert set(again.symbols) == set(panel.symbols)
        # 逐标的对比有效日期的 close
        for s in again.symbols:
            a_hist = again.history(s, again.dates[-1])
            p_hist = panel.history(s, panel.dates[-1])
            merged = a_hist[["close"]].join(p_hist[["close"]], lsuffix="_a", rsuffix="_p", how="inner")
            assert len(merged) == len(a_hist)
            np.testing.assert_allclose(merged["close_a"], merged["close_p"], rtol=1e-10)


class TestMetrics:
    def test_max_drawdown_known_case(self):
        idx = pd.bdate_range("2024-01-01", periods=5)
        eq = pd.Series([100.0, 120.0, 60.0, 80.0, 90.0], index=idx)
        assert max_drawdown(eq) == pytest.approx(0.5)
        dd = drawdown_series(eq)
        assert dd.min() == pytest.approx(-0.5)

    def test_annualized_return(self):
        idx = pd.bdate_range("2023-01-02", periods=245)  # 一年
        eq = pd.Series(np.linspace(100, 110, 245), index=idx)
        class C:
            trading_days_per_year = 244
            risk_free_rate = 0.0
        assert annualized_return(eq, C()) == pytest.approx(0.10, abs=0.01)

    def test_sharpe_flat_curve_zero(self):
        idx = pd.bdate_range("2024-01-01", periods=50)
        eq = pd.Series(100.0, index=idx)
        class C:
            trading_days_per_year = 244
            risk_free_rate = 0.0
        assert sharpe_ratio(daily_returns(eq), C()) == 0.0

    def test_equity_identity(self):
        """任意策略跑完：期末权益 == 初始 + Σ现金流 + 持仓市值（未强平时）。"""
        from quant_sim.core.config import BacktestConfig
        from quant_sim.core.engine import run_backtest
        from quant_sim.strategies import DualMAStrategy

        panel = make_demo_panel(seed=9)
        cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-06-30", initial_cash=500_000)
        r = run_backtest(DualMAStrategy("510500", 10, 30), panel, cfg)
        cashflows = r.fills["cash_flow"].sum()
        # 权益曲线最后一日 = 初始 + 现金流 + 市值
        lhs = r.equity.iloc[-1]
        rhs = 500_000 + cashflows + r.holdings_value.iloc[-1]
        assert lhs == pytest.approx(rhs, abs=1.0)
        assert (r.cash >= -1e-6).all(), "现金不允许透支"
        assert (r.positions >= 0).all().all(), "持仓不允许为负"
