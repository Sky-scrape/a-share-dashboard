"""8/30 设计调整（Tier 1-3）的回归锁：manifest / runner / 报告层 / 配置统一 / WF 防泄漏。"""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quant_sim import BacktestConfig, run_backtest
from quant_sim.core.types import BarPanel
from quant_sim.data import manifest as man
from quant_sim.data.loader import load_panel, save_panel
from quant_sim.research import make_backtest_config
from quant_sim.research.grid_walkforward import grid_search
from quant_sim.research.runner import run_variants  # noqa: F401  供下方用例使用
from quant_sim.strategies import DualMAStrategy
from tests.helpers import flat_rows, make_panel


# ------------------------------------------------------------------ manifest
class TestManifest:
    def test_roundtrip_and_mismatch(self, tmp_path):
        panel = make_panel({"600000": flat_rows(10, 10.0)})
        out = str(tmp_path / "daily")
        paths = save_panel(panel, out)
        assert paths
        # 手工写 manifest：600000 记录为 raw，而调用方期望 qfq → 不一致
        man.update_manifest(out, {"600000.parquet": {"adjust": "raw", "source": "test"}})
        p2 = load_panel(out, expect_adjust="qfq")
        assert p2.metadata.get("manifest_mismatch") == {"600000.parquet": "raw"}
        # 期望一致时不误报
        p3 = load_panel(out, expect_adjust="raw")
        assert not p3.metadata.get("manifest_mismatch")

    def test_check_adjust_only_stock_vocab(self):
        m = {"a.parquet": {"adjust": "qfq"}, "b.parquet": {"adjust": "dividend_reinvested"},
             "c.parquet": {"adjust": "none"}, "d.parquet": {}}
        mism = man.check_adjust(m, "hfq")
        assert mism == {"a.parquet": "qfq"}  # ETF/指数固有口径不参与股票比较；无记录不判

    def test_broken_manifest_not_fatal(self, tmp_path):
        (tmp_path / man.MANIFEST_NAME).write_text("not json{{", encoding="utf-8")
        assert man.read_manifest(str(tmp_path)) == {}


# ------------------------------------------------------------------ runner
class TestRunner:
    def test_run_variants_error_row_and_order(self):
        panel = make_panel({"600000": flat_rows(6, 10.0)})
        cfg = BacktestConfig()
        from quant_sim.research.runner import run_variants

        variants = [{"fast": 2, "slow": 5}, {"fast": 5, "slow": 2}]  # 第二组断言失败
        df, results = run_variants(
            variants,
            build=lambda v: DualMAStrategy("600000", **v),
            panel=panel,
            config=cfg,
            metrics=("累计收益率", "夏普比率"),
        )
        assert len(df) == 2 and len(results) == 2
        assert df["_error"].iloc[1] != "" and results[1] is None
        assert df["_error"].iloc[0] == "" and results[0] is not None

    def test_grid_rank_by_lower_is_better(self):
        """rank_by=最大回撤 时升序偏好（回撤小者靠前），_LOWER_IS_BETTER 路径回归。"""
        rng = np.random.default_rng(5)
        n = 240
        dates = pd.bdate_range("2023-01-02", periods=n)
        close = 10 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
        panel = BarPanel.from_wide({"510300": pd.DataFrame(
            {"open": close, "high": close * 1.005, "low": close * 0.995, "close": close, "volume": 1e8}, index=dates)})
        cfg = BacktestConfig(initial_cash=200_000)
        df = grid_search(lambda **kw: DualMAStrategy("510300", **kw),
                         {"fast": [5, 10, 15], "slow": [30, 60, 90]}, panel, cfg, rank_by="最大回撤")
        ok = df[df["_error"] == ""]
        assert ok["最大回撤"].iloc[0] <= ok["最大回撤"].iloc[-1]

    def test_make_backtest_config_matches_ui_shape(self):
        cfg = make_backtest_config(start="2023-01-01", end="2024-01-01", cash=500000,
                                   execution="next_open", commission=2.5e-4, min_comm=5.0,
                                   stamp=5e-4, slip_model="spread", slip_value=5e-4,
                                   participation=0.05, max_dd_halt=0.2, benchmark="000300")
        assert cfg.initial_cash == 500000
        assert cfg.cost.buy_rate == 2.5e-4 and cfg.cost.stamp_tax_rate_sell == 5e-4
        assert cfg.risk.max_participation_rate == 0.05 and cfg.risk.max_drawdown_halt == 0.2
        assert cfg.benchmark == "000300"


# ------------------------------------------------------------------ 报告层
class TestReportLayer:
    def test_display_value_single_source(self):
        from quant_sim.report.formatter import display_value

        assert display_value("夏普比率", 1.23456) == "1.235"
        assert display_value("累计收益率", 0.1234) == "12.34%"
        assert display_value("总手续费", 1234.5) == "1,234.50 元"
        assert display_value("总手续费", 1234.5, money_unit=False) == "1,234.50"
        assert display_value("交易次数", 1234) == "1,234"
        assert display_value("胜率", float("nan")) == "-"
        assert display_value("Beta", float("inf")) == "∞"
        assert display_value("回测区间", "a ~ b") == "a ~ b"

    def test_performance_display_delegates(self):
        from quant_sim.metrics.performance import _display
        from quant_sim.report.formatter import display_value

        for key, v in [("夏普比率", 1.23456), ("累计收益率", 0.1234), ("总手续费", 1234.5), ("交易次数", 7)]:
            assert _display(key, v) == display_value(key, v)

    def test_save_result_three_formats(self, tmp_path):
        panel = make_panel({"510300": flat_rows(8, 10.0)})
        r = run_backtest(DualMAStrategy("510300", 2, 5), panel, BacktestConfig(initial_cash=200_000))
        paths = r.save(str(tmp_path), name="t", formats=("csv", "json", "html"),
                       data_note="口径说明 <test>")
        import os

        assert len(paths) == 3
        for p in paths:
            assert os.path.exists(p), p
        htmls = list(tmp_path.rglob("t_report.html"))
        assert htmls
        html = htmls[0].read_text(encoding="utf-8")
        assert "__" not in html.replace("__proto__", "")  # 无未填充 token
        assert "口径说明 &lt;test&gt;" in html           # 用户文本已转义
        js = list(tmp_path.rglob("t_metrics.json"))
        assert js and isinstance(json.loads(js[0].read_text(encoding="utf-8")), dict)

    def test_html_survives_nan_metrics(self, tmp_path):
        panel = make_panel({"510300": flat_rows(8, 10.0)})
        r = run_backtest(DualMAStrategy("510300", 2, 5), panel, BacktestConfig(initial_cash=200_000))
        r.metrics["夏普比率"] = float("nan")
        r.metrics["胜率"] = None
        r2 = replace(r, metrics=r.metrics)
        out = r2.save(str(tmp_path), name="nan", formats=("html",))
        html = (tmp_path / "nan_report.html").read_text(encoding="utf-8")
        assert "-" in html and out


# ------------------------------------------------------------------ WF 防泄漏
class TestWalkForwardNoLeak:
    def test_future_data_does_not_change_oos_fold(self):
        """测试窗之后的行情被改动/删除，该折 OOS 指标必须不变（样本外纪律的性质测试）。"""
        from quant_sim.research import walk_forward

        rng = np.random.default_rng(9)
        n = 600
        dates = pd.bdate_range("2022-01-03", periods=n)
        close = 10 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
        frames = {"510300": pd.DataFrame(
            {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close, "volume": 1e8}, index=dates)}
        panel = BarPanel.from_wide(frames)
        cfg = BacktestConfig(initial_cash=200_000, end_date=str(dates[450].date()))
        factory = lambda **kw: DualMAStrategy("510300", **kw)
        grid = {"fast": [5, 10], "slow": [30, 60]}
        res1 = walk_forward(factory, grid, panel, cfg, train_days=200, test_days=100)
        # 把评估窗之外的未来数据整体改掉
        close2 = close.copy()
        close2[451:] = close2[451:] * 0.5
        frames2 = {"510300": pd.DataFrame(
            {"open": close2, "high": close2 * 1.004, "low": close2 * 0.996, "close": close2, "volume": 1e8}, index=dates)}
        panel2 = BarPanel.from_wide(frames2)
        res2 = walk_forward(factory, grid, panel2, cfg, train_days=200, test_days=100)
        pd.testing.assert_frame_equal(res1.folds, res2.folds)
        assert (res1.oos_equity - res2.oos_equity).abs().max() < 1e-9
