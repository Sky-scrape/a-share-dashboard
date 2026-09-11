"""quant_api 接线层回归锁（2026-09-10 修复批次）。

覆盖：
  * 标的池净化 tradable_codes：去重、保序、剔除基准（锁「排序后 panel.symbols
    污染策略池：单标策略取错第一只、指数被塞进可交易池」的回归）。
  * build_strategy_from_desc 基本行为：单标缺省取用户 codes[0]、双均线校验、
    规则池保序、动量标的数下限。
  * 期末强制平仓复用撮合语义：卖出滑点入账、跌停封板保留持仓。
  * 风控缩量 verdict.reason 真实上报（旧实现恒为空串）。
  * run_job 网格解析/变体描述合并（曾 grid/wf 各写一份）。
  * 滑点模型（none/percent/tick/spread）与撮合层 [low,high] 裁剪。
"""

from __future__ import annotations

import os
import sys
import types

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_QUANT = os.path.join(ROOT, "backend", "quant")
for p in (BACKEND_QUANT, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import quant_api  # noqa: E402
import run_job  # noqa: E402

from quant_sim.core.config import BacktestConfig, CostConfig, CostModel, SlippageConfig  # noqa: E402
from quant_sim.core.contract import Contract  # noqa: E402
from quant_sim.core.engine import run_backtest  # noqa: E402
from quant_sim.core.matching import MatchingEngine  # noqa: E402
from quant_sim.core.risk import RiskManager  # noqa: E402
from quant_sim.core.strategy_base import Strategy  # noqa: E402
from quant_sim.core.types import Order, Side  # noqa: E402
from tests.helpers import flat_rows, make_panel  # noqa: E402


# ------------------------------------------------------------------ 标的池净化
class TestTradableCodes:
    def test_dedup_preserve_order_drop_benchmark(self):
        codes = quant_api.tradable_codes(
            ["600000", " 600000 ", "000001", "000300", ""], benchmark="000300")
        assert codes == ["600000", "000001"], "去重保序且剔除基准指数"

    def test_benchmark_only_pool_is_empty(self):
        assert quant_api.tradable_codes(["000300"], benchmark="000300") == []
        assert quant_api.tradable_codes([], "000300") == []

    def test_suffix_form_benchmark_removed(self):
        assert quant_api.tradable_codes(["510300", "000300.SH"], benchmark="000300.SH") == ["510300"]


class TestBuildStrategyFromDesc:
    def test_single_symbol_takes_user_first_code(self):
        """用户池第一只是 600000；排序后的 panel.symbols 会把 000001 顶到前面——回归锁。"""
        strat = quant_api.build_strategy_from_desc(
            {"kind": "builtin", "family": "dual_ma", "params": {"fast": 5, "slow": 20}},
            ["600000", "000001"])
        assert strat.symbol == "600000"

    def test_dual_ma_validates_fast_lt_slow(self):
        with pytest.raises(ValueError, match="fast"):
            quant_api.build_strategy_from_desc(
                {"kind": "builtin", "family": "dual_ma", "params": {"fast": 30, "slow": 10}},
                ["600000"])

    def test_rule_spec_symbols_keep_user_order(self):
        strat = quant_api.build_strategy_from_desc(
            {"kind": "rule", "spec": {}}, ["600000", "000001"])
        assert strat.symbols == ["600000", "000001"]

    def test_momentum_needs_two_symbols(self):
        with pytest.raises(ValueError, match="至少需要 2 只"):
            quant_api.build_strategy_from_desc(
                {"kind": "builtin", "family": "momentum", "params": {}}, ["600000"])

    def test_unknown_family_raises(self):
        with pytest.raises(ValueError, match="未知内置策略族"):
            quant_api.build_strategy_from_desc({"kind": "builtin", "family": "alien"}, ["600000"])


# ------------------------------------------------------------------ 期末强制平仓
def _rows_with_range(n, price=10.0):
    """带日内区间的行情：spread/percent 滑点不会被 [low,high] 裁剪吃掉。"""
    return [{"open": price, "high": price * 1.02, "low": price * 0.98, "close": price, "volume": 1e9}
            for _ in range(n)]


class _BuyAndHold(Strategy):
    acted = False

    def on_bar(self, ctx):
        if not self.acted:
            ctx.buy("600000", quantity=1000)
            self.acted = True


class TestForceLiquidate:
    def test_force_sell_applies_slippage(self):
        """期末强平复用撮合卖出语义：成交价含滑点、slippage_cost 入账（旧实现恒 0）。"""
        panel = make_panel({"600000": _rows_with_range(4)})
        cfg = BacktestConfig(initial_cash=1e6, slippage=SlippageConfig(model="percent", value=0.01))
        r = run_backtest(_BuyAndHold(), panel, cfg)
        forced = r.fills[r.fills["order_id"] < 0]
        assert len(forced) == 1, "期末应有一笔强平卖出"
        f = forced.iloc[0]
        assert "强制平仓" in str(f["reason"])
        assert f["side"] == "sell"
        assert f["raw_price"] == pytest.approx(10.0)
        assert f["price"] == pytest.approx(9.9), "percent 滑点 1%：卖价 10×0.99"
        assert f["slippage_cost"] == pytest.approx(0.1 * f["quantity"])
        # 末日行情收在 10.0：强平后不应再有持仓市值
        assert r.holdings_value.iloc[-1] == 0.0
        assert bool(r.force_liquidated)

    def test_limit_down_keeps_position_valued_at_last_price(self):
        """末日跌停封板：强平不成交，持仓保留并按最后价估值（旧实现无条件硬平）。"""
        rows = flat_rows(4, 10.0)
        # pre_close 10 → 跌停 9.00；全天封死
        rows.append({"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 1e6, "pre_close": 10.0})
        panel = make_panel({"600000": rows})
        cfg = BacktestConfig(initial_cash=1e6, slippage=SlippageConfig(model="none"))
        r = run_backtest(_BuyAndHold(), panel, cfg)
        forced = r.fills[r.fills["order_id"] < 0]
        assert len(forced) == 0, "跌停封板日强平卖单不得成交"
        assert r.holdings_value.iloc[-1] > 0, "保留持仓按最后价估值"
        assert not bool(r.force_liquidated)
        # 净值口径：末日 equity = 现金 + 持仓市值（9 元×持仓），未被拍平
        assert r.equity.iloc[-1] == pytest.approx(r.cash.iloc[-1] + r.holdings_value.iloc[-1])


# ------------------------------------------------------------------ 风控缩量上报
class TestRiskShrinkReason:
    def test_cash_shrink_reports_reason(self):
        """现金不足自动缩量：verdict.reason 必须真实上报「风控缩量」（旧实现恒空）。"""
        from quant_sim.core.config import RiskConfig
        from quant_sim.core.cost import CostModel as CM

        rm = RiskManager(RiskConfig(), Contract(), CM(slippage_model="none"))
        panel = make_panel({"600000": flat_rows(2, 10.0)})
        bar = panel.bars(panel.dates[0])["600000"]
        v = rm.approve(Order(symbol="600000", side=Side.BUY, quantity=100000),
                       cash=10500, cash_buffer_pct=1.0, position_quantity=0,
                       position_available=0, equity=10500, position_value=0,
                       total_holdings_value=0, bar=bar, halted=False, buy_forbidden=False)
        assert v.approved and 0 < v.quantity < 100000
        assert "风控缩量" in v.reason and str(v.quantity) in v.reason


# ------------------------------------------------------------------ run_job 网格解析/变体描述
class TestRunJobGridHelpers:
    def test_parse_grid_strings_and_lists(self):
        g = run_job.parse_grid({"fast": "5, 10，15", "slow": [20, 30], "mode": "a,b"})
        assert g["fast"] == [5, 10, 15]
        assert g["slow"] == [20, 30]
        assert g["mode"] == ["a", "b"]

    def test_parse_grid_empty(self):
        assert run_job.parse_grid(None) == {}
        assert run_job.parse_grid({}) == {}

    def test_merge_variant_desc_builtin_and_rule(self):
        base = {"kind": "builtin", "family": "dual_ma", "params": {"fast": 5, "slow": 20}}
        merged = run_job.merge_variant_desc(base, {"fast": 10})
        assert merged["params"] == {"fast": 10, "slow": 20} and merged["family"] == "dual_ma"
        rule = {"kind": "rule", "spec": {"symbols": ["600000"]}}
        m2 = run_job.merge_variant_desc(rule, {"target_weight": 0.5})
        assert m2 == {"kind": "rule", "spec": {"symbols": ["600000"], "target_weight": 0.5}}

    def test_grid_build_uses_tradable_codes(self, monkeypatch):
        """run_grid 的 build 闭包走用户标的池（旧实现传 panel.symbols 会混入基准）。"""
        from quant_sim.research import runner

        captured = {}

        def fake_build_strategy(desc, codes):
            captured["codes"] = list(codes)
            return "STRAT"

        def fake_run_variants(variants, build, panel, cfg, **k):
            import pandas as pd

            rows = []
            for v in variants:
                try:
                    build(v)
                    rows.append({**v, "_error": ""})
                except Exception as e:  # noqa: BLE001
                    rows.append({**v, "_error": str(e)})
            return pd.DataFrame(rows), []

        monkeypatch.setattr(quant_api, "build_strategy_from_desc", fake_build_strategy)
        # 假 panel：symbols 是排序后含基准的样子（真实 panel.symbols 不再传给策略构建）
        fake_panel = types.SimpleNamespace(symbols=["000300", "000001", "600000"])
        monkeypatch.setattr(quant_api, "load_panel_for_api", lambda *a, **k: fake_panel)
        monkeypatch.setattr(runner, "run_variants", fake_run_variants)
        p = {"codes": ["600000", "000001"], "benchmark": "000300",
             "grids": {"fast": [5]}, "strategy": {"kind": "builtin", "family": "dual_ma"}}
        run_job.run_grid(p, quant_api)
        assert captured["codes"] == ["600000", "000001"], "基准 000300 不得进入策略构建池"


# ------------------------------------------------------------------ 滑点模型
class TestSlippageModels:
    def _cm(self, model, value):
        return CostModel(slippage_model=model, slippage_value=value, spread_share=0.5)

    def test_none_and_percent(self):
        cm = self._cm("none", 0.01)
        assert cm.slippage(10.0, Side.BUY) == 10.0
        cm = self._cm("percent", 0.005)
        assert cm.slippage(10.0, Side.BUY) == pytest.approx(10.05)
        assert cm.slippage(10.0, Side.SELL) == pytest.approx(9.95)

    def test_tick(self):
        cm = self._cm("tick", 3)
        assert cm.slippage(10.0, Side.BUY, tick_size=0.01) == pytest.approx(10.03)
        assert cm.slippage(10.0, Side.SELL, tick_size=0.01) == pytest.approx(9.97)

    def test_spread_uses_bar_range_capped_by_value(self):
        cm = self._cm("spread", 0.005)

        class Bar:
            high, low = 10.4, 9.6

        # (high-low)×share = 0.4 > price×value = 0.05 → 取价×value 封顶
        assert cm.slippage(10.0, Side.BUY, bar=Bar()) == pytest.approx(10.05)
        assert cm.slippage(10.0, Side.SELL, bar=Bar()) == pytest.approx(9.95)

        class NarrowBar:
            high, low = 10.02, 9.98

        # (high-low)×share = 0.02 < 0.05 → 取半个价差
        assert cm.slippage(10.0, Side.BUY, bar=NarrowBar()) == pytest.approx(10.02)

    def test_spread_without_bar_falls_back_to_percent(self):
        cm = self._cm("spread", 0.005)
        assert cm.slippage(10.0, Side.BUY) == pytest.approx(10.05)

    def test_matching_clips_to_low_high(self):
        """撮合层裁剪：滑点后价格不得越出 [low, high]。"""
        rows = [{"open": 10.0, "high": 10.01, "low": 9.99, "close": 10.0, "volume": 1e9}]
        panel = make_panel({"600000": rows})
        bar = panel.bars(panel.dates[0])["600000"]
        eng = MatchingEngine(self._cm("percent", 0.05), Contract(), execution="close")
        fill, status, _ = eng.match_one(Order(symbol="600000", side=Side.BUY, quantity=100), bar, panel.dates[0])
        assert fill is not None
        assert fill.price == pytest.approx(10.01), "5% 滑点被裁剪到当日最高价"
        fill_s, _, _ = eng.match_one(Order(symbol="600000", side=Side.SELL, quantity=100), bar, panel.dates[0])
        assert fill_s.price == pytest.approx(9.99), "卖方滑点被裁剪到当日最低价"


# ------------------------------------------------------------------ hithink 新鲜度快路
class TestHithinkCacheFreshness:
    def _make_cache(self, d):
        """手造本地缓存：600000.parquet（2024-01-01~04）+ manifest 记录。"""
        import json

        d.mkdir(parents=True, exist_ok=True)
        idx = pd.bdate_range("2024-01-01", periods=4)
        df = pd.DataFrame({"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                           "volume": 1e6, "amount": 1e7}, index=idx)
        df.index.name = "date"
        df.to_parquet(d / "600000.parquet")
        manifest = {"600000.parquet": {"adjust": "qfq", "source": "test",
                                       "first": "2024-01-01", "last": "2024-01-04"}}
        (d / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return d

    def test_covered_cache_skips_export(self, tmp_path, monkeypatch):
        from quant_sim.data import hithink

        d = self._make_cache(tmp_path / "daily")

        def boom(*a, **k):
            raise AssertionError("本地已覆盖请求区间时不应再触发导出")

        monkeypatch.setattr(hithink, "export_any", boom)
        panel = hithink.load_panel(["600000"], start="2024-01-01", end="2024-01-04",
                                   auto=True, cache_dir=str(d))
        assert len(panel.dates) == 4
        assert panel.metadata["source"].endswith("+cache"), "命中本地快路应带 +cache 标记"

    def test_uncovered_range_still_exports(self, tmp_path, monkeypatch):
        from quant_sim.data import hithink

        d = self._make_cache(tmp_path / "daily")
        called = []
        monkeypatch.setattr(hithink, "export_any", lambda *a, **k: called.append(a) or {})
        # 请求 end 超出 manifest last → 不覆盖 → 走导出
        hithink.load_panel(["600000"], start="2024-01-01", end="2024-01-31",
                           auto=True, cache_dir=str(d))
        assert called, "区间未被本地覆盖时应照常导出"

    def test_force_refresh_escape(self, tmp_path, monkeypatch):
        from quant_sim.data import hithink

        d = self._make_cache(tmp_path / "daily")
        called = []
        monkeypatch.setattr(hithink, "export_any", lambda *a, **k: called.append(a) or {})
        hithink.load_panel(["600000"], start="2024-01-01", end="2024-01-04",
                           auto=True, cache_dir=str(d), force_refresh=True)
        assert called, "force_refresh=True 应跳过快路强制重导"

    def test_adjust_mismatch_not_covered(self, tmp_path, monkeypatch):
        import json

        from quant_sim.data import hithink

        d = self._make_cache(tmp_path / "daily")
        man = json.loads((d / "_manifest.json").read_text(encoding="utf-8"))
        man["600000.parquet"]["adjust"] = "hfq"   # 请求 qfq → 口径不一致
        (d / "_manifest.json").write_text(json.dumps(man), encoding="utf-8")
        called = []
        monkeypatch.setattr(hithink, "export_any", lambda *a, **k: called.append(a) or {})
        hithink.load_panel(["600000"], start="2024-01-01", end="2024-01-04",
                           auto=True, cache_dir=str(d))
        assert called, "复权口径不一致时不得走本地快路"


# ------------------------------------------------------------------ 费率单一来源与过户费口径
class TestCostDefaultsSingleSource:
    def test_cost_config_matches_cost_model(self):
        """CostConfig 默认值必须与 CostModel 同源（曾两套手写默认值漂移过）。"""
        m, c = CostModel(), CostConfig()
        assert c.buy_rate == m.buy_commission_rate
        assert c.sell_rate == m.sell_commission_rate
        assert c.min_commission == m.min_commission
        assert c.stamp_tax_rate_sell == m.stamp_tax_rate_sell
        assert c.transfer_fee_rate == m.transfer_fee_rate
        assert c.other_fee_rate == m.other_fee_rate
        assert c.fund_rate == m.fund_commission_rate
        assert c.fund_min_commission == m.fund_min_commission

    def test_transfer_fee_single_sided(self):
        """过户费：默认万 0.1 已是双边口径的单边费率——10 万元成交应收 1 元/边（曾 ×2）。"""
        cm = CostModel(slippage_model="none")
        b = cm.costs("600000", Side.BUY, 100000, 1.0)   # 成交额 10 万
        assert b.transfer_fee == pytest.approx(1.0)
        s = cm.costs("600000", Side.SELL, 100000, 1.0)
        assert s.transfer_fee == pytest.approx(1.0)
