"""风控闸门：在订单进入撮合队列前拦截。

规则分层（越靠前越硬）：
  1. 停牌/黑名单/白名单
  2. 整手与数量合法性
  3. 资金预留（买入）/ 持仓与 T+1 可卖量（卖出）
  4. 单一标的上限、总仓位上限、单笔金额上限
  5. 日内亏损限制、最大回撤熔断（熔断后只允许卖出）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import RiskConfig
from .contract import Contract
from .cost import CostModel
from .types import Order, Side

__all__ = ["RiskManager", "RiskEvent", "RiskVerdict"]


@dataclass
class RiskEvent:
    date: object
    kind: str
    symbol: Optional[str]
    message: str
    action: str = "reject"  # reject | halt | force_liquidate


@dataclass
class RiskVerdict:
    approved: bool
    quantity: int = 0
    reason: str = ""


@dataclass
class RiskManager:
    config: RiskConfig
    contract: Contract = field(default_factory=Contract)
    cost_model: Optional[CostModel] = None

    # ------------------------------------------------------------------ 熔断
    def check_circuit_breaker(
        self,
        date,
        equity: float,
        peak_equity: float,
        day_start_equity: float,
        initial_cash: float,
    ) -> List[RiskEvent]:
        events: List[RiskEvent] = []
        dd = 1.0 - equity / peak_equity if peak_equity > 0 else 0.0
        if self.config.max_drawdown_halt and dd > self.config.max_drawdown_halt:
            events.append(
                RiskEvent(
                    date=date,
                    kind="max_drawdown_halt",
                    symbol=None,
                    message=f"回撤 {dd:.2%} 超过阈值 {self.config.max_drawdown_halt:.2%}，清仓并停止开新仓",
                    action="halt",
                )
            )
        if self.config.max_daily_loss_pct and day_start_equity > 0:
            daily_loss = 1.0 - equity / day_start_equity
            if daily_loss > self.config.max_daily_loss_pct:
                events.append(
                    RiskEvent(
                        date=date,
                        kind="max_daily_loss",
                        symbol=None,
                        message=f"当日亏损 {daily_loss:.2%} 超过阈值 {self.config.max_daily_loss_pct:.2%}，禁止开新仓",
                        action="reject_buy",
                    )
                )
        return events

    # ------------------------------------------------------------------ 订单
    def approve(
        self,
        order: Order,
        *,
        cash: float,
        cash_buffer_pct: float = 1.0,
        position_quantity: int,
        position_available: int,
        equity: float,
        position_value: float,
        total_holdings_value: float,
        bar,
        halted: bool,
        buy_forbidden: bool,
    ) -> RiskVerdict:
        cfg = self.config
        cost_rate = self.cost_model.estimated_cost_pct(order.symbol, order.side) if self.cost_model else 0.0006

        if bar is None or not bar.is_valid:
            return RiskVerdict(False, 0, "停牌或无行情")
        if order.symbol in cfg.blacklist:
            return RiskVerdict(False, 0, "标的在黑名单")
        if cfg.whitelist and order.symbol not in cfg.whitelist:
            return RiskVerdict(False, 0, "标的不在白名单")
        if order.quantity <= 0:
            return RiskVerdict(False, 0, "下单数量必须为正")
        closing = False
        qty = int(order.quantity)
        original_qty = qty   # 风控缩量的判定基准：任何 clone 之前的原始申请量
        if order.side is Side.SELL:
            if position_available <= 0:
                return RiskVerdict(False, 0, "无可卖持仓（T+1 限制或空仓）")
            if qty > position_available:
                if qty >= position_quantity:
                    # 意图清仓：把当前可卖部分（含零股）全部卖出
                    qty = position_available
                    closing = True
                    order = _clone(order, qty)
                else:
                    return RiskVerdict(False, 0, f"可卖不足（T+1）：申请 {qty}，可卖 {position_available}")
        if halted and order.side is Side.BUY:
            return RiskVerdict(False, 0, "风控熔断中，只允许平仓")
        if buy_forbidden and order.side is Side.BUY:
            return RiskVerdict(False, 0, "日内亏损限制，暂停开新仓")
        if not closing and cfg.max_order_amount:
            max_by_amount = int(cfg.max_order_amount / (bar.close * (1 + cost_rate)))
            if qty > max_by_amount:
                qty = self.contract.round_quantity(max_by_amount)
                if qty <= 0:
                    return RiskVerdict(False, 0, f"单笔金额上限 {cfg.max_order_amount:,.0f} 元，无法下单")
                order = _clone(order, qty)
        if cfg.max_gross_exposure_pct and order.side is Side.BUY:
            room = equity * cfg.max_gross_exposure_pct - total_holdings_value
            if room <= 0:
                return RiskVerdict(False, 0, f"总仓位已达上限 {cfg.max_gross_exposure_pct:.0%}")
            max_qty = int(room / (bar.close * (1 + cost_rate)))
            max_qty = self.contract.round_quantity(min(qty, max_qty))
            if max_qty <= 0:
                return RiskVerdict(False, 0, "剩余仓位额度不足 1 手")
            if max_qty < qty:
                qty = max_qty
                order = _clone(order, qty)
        if cfg.max_position_pct and order.side is Side.BUY:
            room = equity * cfg.max_position_pct - position_value
            if room <= 0:
                return RiskVerdict(False, 0, f"{order.symbol} 仓位已达上限 {cfg.max_position_pct:.0%}")
            max_qty = self.contract.round_quantity(min(qty, int(room / (bar.close * (1 + cost_rate)))))
            if max_qty <= 0:
                return RiskVerdict(False, 0, "单标的仓位额度不足 1 手")
            if max_qty < qty:
                qty = max_qty
                order = _clone(order, qty)
        if order.side is Side.BUY:
            # cash_buffer_pct（BacktestConfig.cash_demand_pct，默认 1.002）：在「收盘价+费率」预估
            # 之外再留缓冲，覆盖次日开盘跳空/最低佣金尾差，避免成交阶段才发现现金不够而静默拒单。
            price_est = bar.close * (1 + cost_rate) * max(1.0, float(cash_buffer_pct))
            affordable = int(cash / (price_est or 1.0))
            affordable = self.contract.round_quantity(min(qty, affordable))
            if affordable <= 0:
                return RiskVerdict(False, 0, f"现金不足：约需 {qty * price_est:,.2f}，可用 {cash:,.2f}")
            qty = affordable
        # 先与原始申请量比较、再 clone：旧写法 clone 后才判 `qty < order.quantity`
        # 恒为 False，「风控缩量」的 reason 永远上不了报。
        shrunk = qty < original_qty
        order = _clone(order, qty)
        if shrunk:
            return RiskVerdict(True, qty, f"风控缩量至 {qty}")
        return RiskVerdict(True, qty, "")

    def validate_quantity(self, symbol: str, quantity: float, closing: bool = False) -> int:
        return self.contract.round_quantity(quantity, closing=closing)


def _clone(order: Order, quantity: int) -> Order:
    new = Order(
        symbol=order.symbol,
        side=order.side,
        quantity=int(quantity),
        order_type=order.order_type,
        limit_price=order.limit_price,
        reason=order.reason,
    )
    new.id = order.id
    new.created_date = order.created_date
    new.status = order.status
    return new
