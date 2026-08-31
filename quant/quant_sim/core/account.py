"""账户与持仓：现金、T+1 可卖数量、FIFO 批次、已实现/浮动盈亏。

会计口径：
  * 买入：现金 = -(成交额 + 费用)；新建 Lot，available=0（当日不可卖）
  * 卖出：按 FIFO 从 available>0 的批次扣减；现金 = 成交额 - 费用
  * 每个交易日开始时把全部 Lot.available 置为 quantity（等价 T+1 解锁）
  * 权益 = 现金 + Σ(持仓数量 × 当日收盘价)，已含买入费用（费用沉在成本里）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contract import Contract
from .types import Fill, Lot, Position, Side, Trade

__all__ = ["Account"]


@dataclass
class Account:
    initial_cash: float
    contract: Contract = field(default_factory=lambda: Contract())
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)
    total_commission: float = 0.0
    total_stamp_tax: float = 0.0
    total_transfer_fee: float = 0.0
    total_other_fee: float = 0.0
    total_slippage: float = 0.0
    realized_pnl: float = 0.0
    _date_seq: Dict = field(default_factory=dict)
    _trade_seq: int = 0

    def __post_init__(self) -> None:
        self.cash = float(self.initial_cash)

    # ------------------------------------------------------------------ 查询
    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position(symbol=symbol)

    def has_position(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return bool(pos and pos.quantity > 0)

    def available(self, symbol: str) -> int:
        return self.position(symbol).available

    def holdings_value(self, prices: Dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            if pos.quantity <= 0:
                continue
            price = prices.get(symbol)
            if price is None or price <= 0:
                price = pos.avg_cost  # 停牌按成本估值
            total += pos.quantity * price
        return total

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.holdings_value(prices)

    def total_costs(self) -> float:
        return self.total_commission + self.total_stamp_tax + self.total_transfer_fee + self.total_other_fee

    def turnover(self) -> float:
        return float(sum(f.gross_amount for f in self.fills))

    # ------------------------------------------------------------------ 事件
    def on_new_day(self, date) -> None:
        """T+1 解锁：所有历史批次当日可卖。"""
        self._date_seq[date] = len(self._date_seq)
        for pos in self.positions.values():
            for lot in pos.lots:
                lot.available = lot.quantity

    def can_apply(self, fill: Fill) -> Optional[str]:
        if fill.side is Side.BUY:
            if self.cash + fill.cash_flow < -1e-6:
                return f"现金不足：需要 {-fill.cash_flow:,.2f}，可用 {self.cash:,.2f}"
        else:
            pos = self.position(fill.symbol)
            if pos.available < fill.quantity:
                return f"可卖不足（T+1）：需要 {fill.quantity}，可卖 {pos.available}"
        return None

    def apply_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.total_commission += fill.commission
        self.total_stamp_tax += fill.stamp_tax
        self.total_transfer_fee += fill.transfer_fee
        self.total_other_fee += fill.other_fee
        self.total_slippage += fill.slippage_cost
        self.cash += fill.cash_flow
        if fill.side is Side.BUY:
            self._apply_buy(fill)
        else:
            self._apply_sell(fill)
        pos = self.positions.get(fill.symbol)
        if pos is not None and pos.quantity <= 0:
            self.positions.pop(fill.symbol, None)

    def _apply_buy(self, fill: Fill) -> None:
        pos = self.positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        # 买入费用计入该批次成本，平仓时随之转入已实现盈亏
        pos.lots.append(
            Lot(
                date=fill.date,
                price=fill.price,
                quantity=fill.quantity,
                cost=fill.total_cost,
                # T+1 开关真实接线：t_plus_1=True 当日买入不可卖；False（如场内 T+0 品种）当日即可卖
                available=0 if self.contract.t_plus_1 else fill.quantity,
                order_id=fill.order_id,
            )
        )
        pos.quantity += fill.quantity

    def _apply_sell(self, fill: Fill) -> None:
        pos = self.positions.get(fill.symbol)
        if pos is None or pos.quantity <= 0:
            if self.contract.allow_short_selling:
                raise NotImplementedError(
                    "allow_short_selling=True 尚未支持：Account 会计为 long-only（Lot/Position 无方向），"
                    "接入融券/对冲前先改造批次方向与保证金模型"
                )
            raise RuntimeError(f"卖出 {fill.symbol} 但无持仓，引擎状态异常")
        remaining = fill.quantity
        sell_amount = fill.gross_amount
        sell_cost_total = fill.total_cost
        seq = self._date_seq.get(fill.date, -1)
        while remaining > 0:
            lot = next((l for l in pos.lots if l.available > 0), None)
            if lot is None:
                raise RuntimeError(f"{fill.symbol} 可卖批次不足，剩余 {remaining}")
            take = min(lot.available, remaining)
            ratio = take / lot.quantity
            entry_cost = lot.cost * ratio
            exit_cost = sell_cost_total * (take / fill.quantity)
            gross_pnl = (fill.price - lot.price) * take
            net_pnl = gross_pnl - entry_cost - exit_cost
            self._trade_seq += 1
            entry_seq = self._date_seq.get(lot.date, seq)
            self.trades.append(
                Trade(
                    symbol=fill.symbol,
                    entry_date=lot.date,
                    exit_date=fill.date,
                    entry_price=lot.price,
                    exit_price=fill.price,
                    quantity=take,
                    gross_pnl=gross_pnl,
                    cost=entry_cost + exit_cost,
                    net_pnl=net_pnl,
                    return_pct=net_pnl / (lot.price * take + entry_cost) if take else 0.0,
                    holding_days=int((fill.date - lot.date).days),
                    holding_bars=max(0, seq - entry_seq),
                    entry_order_id=lot.order_id,
                    exit_order_id=fill.order_id,
                )
            )
            self.realized_pnl += net_pnl
            lot.available -= take
            lot.quantity -= take
            lot.cost -= entry_cost
            remaining -= take
        pos.quantity -= fill.quantity
        pos.lots = [l for l in pos.lots if l.quantity > 0]
