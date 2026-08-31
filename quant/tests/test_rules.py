"""核心交易规则测试：T+1、整手、涨跌停、参与率、资金约束、未来函数。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_sim.core.account import Account
from quant_sim.core.config import BacktestConfig, CostConfig, RiskConfig
from quant_sim.core.contract import Contract
from quant_sim.core.engine import BacktestEngine, run_backtest
from quant_sim.core.strategy_base import Strategy
from quant_sim.core.types import Fill, Lot, Order, Side
from tests.helpers import flat_rows, make_panel


# ------------------------------------------------------------------ 规则层
class TestContract:
    def test_board_limit_pct(self):
        c = Contract()
        assert c.limit_pct("600519") == 0.10
        assert c.limit_pct("300750") == 0.20
        assert c.limit_pct("688001") == 0.20
        assert c.limit_pct("600519", is_st=True) == 0.05
        assert c.limit_pct("920001") == 0.30  # 北交所

    def test_limit_price_rounding(self):
        c = Contract()
        assert c.limit_up_price(10.00, "600000") == 11.00
        assert c.limit_down_price(10.00, "600000") == 9.00
        assert c.limit_up_price(3.33, "600000") == round(3.33 * 1.1, 2) == 3.66
        assert c.limit_down_price(2.71, "600000") == 2.44  # 2.439 → 四舍五入 2.44

    def test_lot_rounding(self):
        c = Contract()
        assert c.round_quantity(1234) == 1200
        assert c.round_quantity(99) == 0
        assert c.round_quantity(150, holding=150, closing=True) == 150  # 零股一次性卖出


    def test_is_fund(self):
        c = Contract()
        assert c.is_fund("510300")
        assert c.is_fund("159915")
        assert not c.is_fund("600519")


# ------------------------------------------------------------------ 账户层
class TestAccountT1:
    def _buy_fill(self, acct, date, qty=1000, price=10.0):
        f = Fill(order_id=1, symbol="600000", side=Side.BUY, date=date, quantity=qty,
                 price=price, raw_price=price, gross_amount=qty * price,
                 commission=5.0, stamp_tax=0, transfer_fee=0.1, other_fee=0,
                 slippage_cost=0, cash_flow=-(qty * price + 5.1))
        return f

    def test_same_day_sell_blocked(self):
        acct = Account(initial_cash=100000)
        d = pd.Timestamp("2024-01-02")
        acct.on_new_day(d)
        f = self._buy_fill(acct, d)
        acct.apply_fill(f)
        assert acct.position("600000").quantity == 1000
        assert acct.position("600000").available == 0  # T+1 锁定
        sell = Fill(order_id=2, symbol="600000", side=Side.SELL, date=d, quantity=1000,
                    price=10.0, raw_price=10.0, gross_amount=10000,
                    commission=5.0, stamp_tax=5.0, transfer_fee=0.1, other_fee=0,
                    slippage_cost=0, cash_flow=9989.9)
        assert acct.can_apply(sell) is not None

    def test_next_day_sell_ok(self):
        acct = Account(initial_cash=100000)
        d1, d2 = pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")
        acct.on_new_day(d1)
        acct.apply_fill(self._buy_fill(acct, d1))
        acct.on_new_day(d2)  # 新交易日解锁
        assert acct.position("600000").available == 1000
        sell = Fill(order_id=2, symbol="600000", side=Side.SELL, date=d2, quantity=1000,
                    price=11.0, raw_price=11.0, gross_amount=11000,
                    commission=5.0, stamp_tax=5.5, transfer_fee=0.11, other_fee=0,
                    slippage_cost=0, cash_flow=10989.39)
        assert acct.can_apply(sell) is None
        acct.apply_fill(sell)
        assert acct.position("600000").quantity == 0
        assert len(acct.trades) == 1
        t = acct.trades[0]
        assert t.net_pnl == pytest.approx(1000 - 5.1 - 10.61)  # 价差 - 买卖双边费用
        assert acct.realized_pnl == t.net_pnl


# ------------------------------------------------------------------ 引擎层
class TestEngineRules:
    def test_buy_100_lot_and_costs(self):
        """双均线买入：整手、费用、成交日正确（next_open：T 日信号 → T+1 开盘）。"""
        panel = make_panel({"600000": flat_rows(120, 10.0)})
        cfg = BacktestConfig(initial_cash=20000)

        class BuyOnce(Strategy):
            name = "buy_once"
            acted = False
            def on_bar(self, ctx):
                if not self.acted and ctx.holding("600000") == 0 and not ctx.pending:
                    ctx.buy("600000", quantity=1500)
                    self.acted = True

        r = run_backtest(BuyOnce(), panel, cfg)
        normal = r.fills[r.fills["order_id"] >= 0]  # 排除期末强制平仓
        assert len(normal) == 1
        f = normal.iloc[0]
        assert int(f["quantity"]) == 1500
        # 信号日 = 第 1 个交易日（day0 无 pre_close… 有行情即可交易），成交在次日开盘
        sig_day, fill_day = panel.dates[0], panel.dates[1]
        assert pd.Timestamp(f["date"]) == fill_day
        gross = 1500 * f["price"]
        assert f["commission"] == max(gross * 2.5e-4, 5.0)  # 股票最低 5 元
        assert f["stamp_tax"] == 0  # 买入无印花税
        assert f["transfer_fee"] > 0
        # 现金恒等式：期末权益 = 初始 + Σ现金流 + 持仓市值
        assert r.equity.iloc[-1] == pytest.approx(20000 + r.fills["cash_flow"].sum() + r.holdings_value.iloc[-1])

    def test_t1_engine_level(self):
        """close 模式：同一 on_bar 里先买后卖 → 卖单被 T+1/无持仓拒掉。"""
        panel = make_panel({"600000": flat_rows(6, 10.0)})
        cfg = BacktestConfig(execution="close")

        class BuySell(Strategy):
            acted = False
            def on_bar(self, ctx):
                if not self.acted:
                    ctx.buy("600000", quantity=1000)
                    ctx.sell("600000", quantity=1000)
                    self.acted = True

        r = run_backtest(BuySell(), panel, cfg)
        assert (r.orders["status"] == "rejected").sum() >= 1
        rejected_sell = r.orders[(r.orders["side"] == "sell") & (r.orders["status"] == "rejected")]
        assert len(rejected_sell) == 1
        assert "可卖" in rejected_sell.iloc[0]["reject_reason"] or "T+1" in rejected_sell.iloc[0]["reject_reason"]
        normal = r.fills[r.fills["order_id"] >= 0]
        assert len(normal) == 1 and normal.iloc[0]["side"] == "buy"

    def test_limit_up_blocks_buy(self):
        """一字涨停买入被拒：pre_close 10 → 全天封死 11.00。"""
        rows = flat_rows(3, 10.0)
        rows.append({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1e6, "pre_close": 10.0})
        rows += flat_rows(2, 11.0)
        panel = make_panel({"600000": rows})
        cfg = BacktestConfig(initial_cash=100000, block_limit_move=True)

        class BuyLate(Strategy):
            def on_bar(self, ctx):
                # 在涨停前一日提交，订单在涨停日（一字板）撮合 → 被拒
                if ctx.date == panel.dates[2] and not ctx.pending and ctx.holding("600000") == 0:
                    ctx.buy("600000", quantity=1000)

        r = run_backtest(BuyLate(), panel, cfg)
        assert len(r.fills) == 0
        assert any("涨停" in s for s in r.orders["reject_reason"].dropna())

    def test_participation_cap_partial(self):
        """参与率限制：当日 100 万股 × 5% → 最多成交 5 万，剩余部分成交。"""
        rows = flat_rows(4, 10.0, volume=1e6)
        panel = make_panel({"600000": rows})
        cfg = BacktestConfig(initial_cash=10_000_000)

        class Big(Strategy):
            acted = False
            def on_bar(self, ctx):
                if not self.acted:
                    ctx.buy("600000", quantity=100000)
                    self.acted = True

        r = run_backtest(Big(), panel, cfg)
        normal = r.fills[r.fills["order_id"] >= 0]
        assert len(normal) == 1
        assert int(normal.iloc[0]["quantity"]) == 50000
        assert r.orders.iloc[0]["status"] == "partial"

    def test_cash_constraint(self):
        """买超现金 → 风控自动缩量到可负担整手数。"""
        panel = make_panel({"600000": flat_rows(4, 10.0)})
        cfg = BacktestConfig(initial_cash=10500)

        class Greedy(Strategy):
            acted = False
            def on_bar(self, ctx):
                if not self.acted:
                    ctx.buy("600000", quantity=100000)  # 需要 100 万
                    self.acted = True

        r = run_backtest(Greedy(), panel, cfg)
        normal = r.fills[r.fills["order_id"] >= 0]
        assert len(normal) >= 1
        assert int(normal.iloc[0]["quantity"]) <= 1000  # 10500 元最多约 1000 股
        assert r.cash.iloc[-1] >= 0

    def test_no_lookahead_history(self):
        """ctx.history 默认截至昨日：今日暴涨不进入信号历史。"""
        rows = flat_rows(70, 10.0)
        # day 60：暴涨 10% 封板（对信号不可见，因为信号只用截至 day59）
        rows[60] = {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1e9}
        panel = make_panel({"600000": rows})

        class Peek(Strategy):
            result = {}
            def on_bar(self, ctx):
                if ctx.date == panel.dates[60]:
                    hist = ctx.history("600000", 5)
                    self.result["last"] = hist.index[-1]
                    self.result["last_close"] = hist["close"].iloc[-1]

        r = run_backtest(Peek(), panel, BacktestConfig())
        assert r.panel is not None
        # 在测试外部再取一次
        eng_results = {}

        class Peek2(Strategy):
            def on_bar(self, ctx):
                if ctx.date == panel.dates[60]:
                    eng_results["last"] = ctx.history("600000", 5).index[-1]
                    eng_results["last_close"] = ctx.history("600000", 5)["close"].iloc[-1]

        run_backtest(Peek2(), panel, BacktestConfig())
        assert eng_results["last"] == panel.dates[59]      # 不含当日
        assert eng_results["last_close"] == pytest.approx(10.0)  # 看不到当日暴涨

    def test_drawdown_halt_liquidates(self):
        """最大回撤熔断：触发后清仓并拒绝新开仓。"""
        # 构造持续阴跌到回撤 >20% 的行情
        px = 10.0
        rows = []
        for i in range(120):
            step = 1.0 if i < 5 else 0.985  # 第 5 天起每天 -1.5%
            o = px
            px = px * step
            rows.append({"open": o, "high": o, "low": px, "close": px, "volume": 1e9})
        panel = make_panel({"600000": rows})
        cfg = BacktestConfig(risk=RiskConfig(max_drawdown_halt=0.15))

        class AlwaysHold(Strategy):
            def on_bar(self, ctx):
                if ctx.holding("600000") == 0 and not ctx.pending:
                    ctx.target_percent("600000", 0.9)

        r = run_backtest(AlwaysHold(), panel, cfg)
        kinds = [e.kind for e in r.risk_events]
        assert "max_drawdown_halt" in kinds
        assert r.positions.iloc[-1].sum() == 0  # 熔断后保持空仓

    def test_stamptax_on_sell_only(self):
        panel = make_panel({"600000": flat_rows(6, 10.0)})
        cfg = BacktestConfig(execution="close")

        class BS(Strategy):
            step = 0
            def on_bar(self, ctx):
                if self.step == 0:
                    ctx.buy("600000", quantity=1000)
                elif self.step == 2:  # T+2 卖出（T+1 已解锁）
                    if ctx.available("600000") >= 1000:
                        ctx.sell("600000", quantity=1000)
                        self.step = 99
                self.step += 1

        r = run_backtest(BS(), panel, cfg)
        normal = r.fills[r.fills["order_id"] >= 0]
        buys = normal[normal["side"] == "buy"]
        sells = normal[normal["side"] == "sell"]
        assert (buys["stamp_tax"] == 0).all()
        assert (sells["stamp_tax"] > 0).all()


# ------------------------------------------------------------------ 撮合层
class TestMatching:
    def test_limit_order_not_filled_when_far(self):
        from quant_sim.core.cost import CostModel
        from quant_sim.core.matching import MatchingEngine
        panel = make_panel({"600000": flat_rows(3, 10.0)})
        bars = panel.bars(panel.dates[1])
        eng = MatchingEngine(CostModel(slippage_model="none"), Contract(), execution="next_open")
        o = Order(symbol="600000", side=Side.BUY, quantity=100, order_type="limit", limit_price=9.0)
        fill, status, msg = eng.match_one(o, bars["600000"], panel.dates[1])
        assert fill is None and status.value == "cancelled"

    def test_limit_order_filled_at_limit_when_better(self):
        from quant_sim.core.cost import CostModel
        from quant_sim.core.matching import MatchingEngine
        rows = [{"open": 9.5, "high": 9.8, "low": 9.4, "close": 9.6, "volume": 1e9}]
        panel = make_panel({"600000": rows})
        bar = panel.bars(panel.dates[0])["600000"]
        eng = MatchingEngine(CostModel(slippage_model="none"), Contract(), execution="next_open")
        o = Order(symbol="600000", side=Side.BUY, quantity=100, order_type="limit", limit_price=9.7)
        fill, status, msg = eng.match_one(o, bar, panel.dates[0])
        assert fill is not None
        assert fill.price <= 9.7  # 开盘 9.5 更优，按更优价成交
