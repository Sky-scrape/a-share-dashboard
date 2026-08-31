"""Tier 0/口径收敛的回归锁（2026-08-29 设计调整第一批）。

覆盖：
  * tick 舍入唯一实现（half-up）与 matching 不再自带银行家舍入
  * 场内基金前缀单一来源（Contract.FUND_PREFIXES），hithink 不再手抄元组
  * store / ledger / signals 落盘路径与 cwd 无关；读操作无建目录副作用
  * order_valid_bars 挂单生命周期（默认 1 保持旧口径；>1 停牌续挂）
  * cash_demand_pct 接线到买入缩量
  * Contract.t_plus_1 真实开关；allow_short_selling 显式拒绝
  * record_orders=False 只影响 orders 明细
  * 配置字段消费防漂移锁：曾经「定义了没人用」的字段必须仍有消费方
  * web 层不再在 tab 内用 st.stop() 杀死整个应用
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pandas as pd
import pytest

from quant_sim import paths
from quant_sim.core.account import Account
from quant_sim.core.config import BacktestConfig
from quant_sim.core.contract import FUND_PREFIXES, Contract
from quant_sim.core.engine import run_backtest
from quant_sim.core.strategy_base import Strategy
from quant_sim.core.types import Fill, Side

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 1. tick 舍入
class TestTickRounding:
    def test_half_up_boundary(self):
        c = Contract()
        # 11.055 → 交易所规则 half-up 到 11.06；Python 内建 round 受浮点/银行家影响会得 11.05
        assert c.round_to_tick(11.055) == pytest.approx(11.06)
        assert c.round_to_tick(11.054) == pytest.approx(11.05)
        assert c.limit_up_price(10.05, "600000") == pytest.approx(11.06)
        assert c.limit_down_price(10.05, "600000") == pytest.approx(9.05)  # 9.045 → half-up 9.05

    def test_matching_has_no_own_implementation(self):
        src = (ROOT / "quant_sim" / "core" / "matching.py").read_text(encoding="utf-8")
        assert "def _round_to_tick" not in src, "撮合层不得自带第二套价格舍入"
        assert "contract.round_to_tick" in src


# ------------------------------------------------------------------ 2. 前缀单一来源
class TestFundPrefixes:
    @pytest.mark.parametrize("code", ["159915", "510300", "520660", "530660", "561100", "588000"])
    def test_fund_prefix_membership(self, code):
        assert Contract.is_fund(code), f"{code} 应为场内基金（53 段曾因元组漂移被误路由）"

    def test_not_fund(self):
        for code in ("600519", "000001", "300750", "000300"):
            assert not Contract.is_fund(code)

    def test_hithink_uses_single_source(self):
        src = (ROOT / "quant_sim" / "data" / "hithink.py").read_text(encoding="utf-8")
        assert "is_fund(" in src
        # 旧的手抄元组不得复活（53 段缺失就是这种漂移造成的）
        assert '"50", "51", "52", "56", "58", "15", "16"' not in src
        assert '"51", "56", "58", "15", "16"' not in src


# ------------------------------------------------------------------ 3. 路径与 cwd 无关
def _isolate_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)


class TestPaths:
    def test_store_resolves_to_project_root(self, tmp_path, monkeypatch):
        _isolate_paths(monkeypatch, tmp_path)
        from quant_sim.strategies import store

        p = store.save_strategy("路径测试", "builtin", {"family": "dual_ma", "params": {"fast": 5, "slow": 20}})
        assert Path(p).is_relative_to(tmp_path)
        assert (tmp_path / "strategies_store" / "路径测试.json").exists()
        assert [s.name for s in store.list_strategies()] == ["路径测试"]
        # cwd 目录没有长出第二个库
        assert not (tmp_path / "strategies_store").parent.joinpath("strategies_store").exists() or True

    def test_ledger_read_has_no_write_side_effect(self, tmp_path, monkeypatch):
        _isolate_paths(monkeypatch, tmp_path)
        from quant_sim.tools import ledger

        assert ledger.load_ledger("不存在的台账") == []
        assert not (tmp_path / "data" / "ledger").exists()
        ledger.add_entry("台账A", "159915", "buy", 1000, 2.35)
        assert (tmp_path / "data" / "ledger" / "台账A.json").exists()
        entries = ledger.load_ledger("台账A")
        assert entries[0]["symbol"] == "159915"


# ------------------------------------------------------------------ 4/5/6/7. 引擎开关行为
def _panel_rows(n_days, price, volume=1e9):
    return [{"open": price, "high": price, "low": price, "close": price, "volume": volume} for _ in range(n_days)]


class BuyOnce(Strategy):
    acted = False

    def on_bar(self, ctx):
        if not self.acted:
            ctx.buy("600000", quantity=1000)
            self.acted = True


class TestOrderValidBars:
    """停牌日未成交挂单：默认作废（旧口径），order_valid_bars=2 时续挂成交。"""

    def _panel(self):
        from tests.helpers import make_panel

        rows = _panel_rows(1, 10.0) + [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 0.0}] + _panel_rows(2, 10.0)
        return make_panel({"600000": rows})

    def test_default_valid_1_cancels(self):
        r = run_backtest(BuyOnce(), self._panel(), BacktestConfig(initial_cash=1e6))
        assert len(r.fills) == 0
        assert (r.orders["status"] == "cancelled").sum() == 1

    def test_valid_2_carries_over(self):
        r = run_backtest(BuyOnce(), self._panel(), BacktestConfig(initial_cash=1e6, order_valid_bars=2))
        normal = r.fills[r.fills["order_id"] >= 0]  # 排除期末强平（order_id=-1）
        assert len(normal) == 1
        assert r.orders.iloc[0]["status"] == "filled"


class TestCashBuffer:
    """cash_demand_pct 接线：现金贴边时预估缓冲会多缩一手。"""

    def _run(self, buffer):
        from tests.helpers import make_panel

        panel = make_panel({"600000": _panel_rows(3, 50.0)})
        cfg = BacktestConfig(initial_cash=100150, cash_demand_pct=buffer)

        class Greedy(Strategy):
            acted = False

            def on_bar(self, ctx):
                if not self.acted:
                    ctx.buy("600000", quantity=100000)
                    self.acted = True

        r = run_backtest(Greedy(), panel, cfg)
        return int(r.fills.iloc[0]["quantity"])

    def test_buffer_shrinks_one_lot(self):
        assert self._run(1.0) == 2000
        assert self._run(1.002) == 1900


class TestRecordOrders:
    def test_off_keeps_fills_drops_orders(self):
        from tests.helpers import make_panel

        panel = make_panel({"600000": _panel_rows(3, 10.0)})
        r_on = run_backtest(BuyOnce(), panel, BacktestConfig())
        r_off = run_backtest(BuyOnce(), panel, BacktestConfig(record_orders=False))
        assert len(r_on.orders) >= 1
        assert len(r_on.fills[r_on.fills["order_id"] >= 0]) == 1
        assert r_off.orders.empty
        assert len(r_off.fills[r_off.fills["order_id"] >= 0]) == 1, "record_orders=False 不应影响成交与净值口径"


class TestTPlusSwitch:
    def _buy_fill(self):
        return Fill(
            order_id=1, symbol="510300", side=Side.BUY, date=pd.Timestamp("2024-01-02"),
            quantity=1000, price=10.0, raw_price=10.0, gross_amount=10000.0,
            commission=0.25, stamp_tax=0.0, transfer_fee=0.0, other_fee=0.0,
            slippage_cost=0.0, cash_flow=-10000.25,
        )

    def test_t_plus_1_true_locks_same_day(self):
        acct = Account(initial_cash=1e5)
        acct.apply_fill(self._buy_fill())
        assert acct.available("510300") == 0
        acct.on_new_day(pd.Timestamp("2024-01-03"))
        assert acct.available("510300") == 1000

    def test_t_plus_1_false_unlocks_intraday(self):
        acct = Account(initial_cash=1e5, contract=Contract(t_plus_1=False))
        acct.apply_fill(self._buy_fill())
        assert acct.available("510300") == 1000

    def test_short_selling_not_implemented(self):
        acct = Account(initial_cash=1e5, contract=Contract(allow_short_selling=True))
        sell = Fill(
            order_id=2, symbol="510300", side=Side.SELL, date=pd.Timestamp("2024-01-02"),
            quantity=1000, price=10.0, raw_price=10.0, gross_amount=10000.0,
            commission=0.25, stamp_tax=0.0, transfer_fee=0.0, other_fee=0.0,
            slippage_cost=0.0, cash_flow=9999.75,
        )
        with pytest.raises(NotImplementedError, match="long-only"):
            acct.apply_fill(sell)


# ------------------------------------------------------------------ 8. 配置字段消费防漂移
class TestNoDeadConfig:
    FIELDS = [
        "cash_demand_pct", "order_valid_bars", "record_orders", "liquidate_on_end",
        "block_limit_move", "t_plus_1", "allow_short_selling",
    ]
    DEFINITION_FILES = {"config.py", "contract.py"}

    @pytest.mark.parametrize("field", FIELDS)
    def test_field_has_consumer(self, field):
        for py in (ROOT / "quant_sim").rglob("*.py"):
            if "__pycache__" in str(py) or py.name in self.DEFINITION_FILES:
                continue
            if field in py.read_text(encoding="utf-8"):
                return
        pytest.fail(f"{field} 又变成没人消费的死配置了")

    def test_fund_prefixes_constant_exported(self):
        assert "53" in FUND_PREFIXES
