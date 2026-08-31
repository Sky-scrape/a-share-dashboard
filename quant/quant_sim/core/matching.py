"""撮合引擎：把订单在给定 bar 上变成成交（或拒单）。

纯函数式实现，不直接改账户状态，便于单测与复用（回测/模拟盘共用）。

MVP 撮合假设（**这是回测结果失真的第一来源，务必理解并可调**）：
  1. 市价单基准价 = 撮合 bar 的开盘价（execution=next_open）或收盘价（execution=close）。
  2. 滑点后再裁剪到 [low, high]，并保证不超过涨停价、不低于跌停价。
  3. 主板/创业板/科创板按规则算涨跌停；触及涨停封板 → 买单不成交；跌停封板 → 卖单不成交。
  4. 单边可成交量上限 = 当日成交量 × 参与率（默认 5%），超出部分当日作废。
  5. 停牌（volume<=0 或无行情）→ 订单作废。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import math

from .contract import Contract
from .cost import CostModel
from .types import Bar, Fill, Order, OrderStatus, Side

__all__ = ["MatchingEngine", "MatchResult"]


@dataclass
class MatchResult:
    fills: List[Fill]
    updates: List[Tuple[Order, OrderStatus, int, str]]  # (order, status, filled_qty, message)


class MatchingEngine:
    def __init__(
        self,
        cost_model: CostModel,
        contract: Contract,
        execution: str = "next_open",
        max_participation_rate: float = 0.05,
        block_limit_move: bool = True,
    ) -> None:
        self.cost_model = cost_model
        self.contract = contract
        self.execution = execution
        self.max_participation_rate = max_participation_rate
        self.block_limit_move = block_limit_move

    # ------------------------------------------------------------------ 工具
    # 价格舍入唯一实现在 Contract.round_to_tick（half-up）；此处不再自备一份
    # 银行家舍入，避免涨停价与成交价在 0.005 边界上口径不一致。

    def base_price(self, order: Order, bar: Bar) -> float:
        # 价格锚点当前仅两个日线假设；模拟盘/分钟线需要在此抽象 ExecutionModel
        # （vwap/日内路径触及），引擎入口已对日内面板硬拦（engine.run 频率护栏）。
        return bar.open if self.execution == "next_open" else bar.close

    # ------------------------------------------------------------------ 主入口
    def match(self, orders: List[Order], bars: dict, date) -> MatchResult:
        fills: List[Fill] = []
        updates: List[Tuple[Order, OrderStatus, int, str]] = []
        for order in orders:
            bar = bars.get(order.symbol)
            if bar is None or not bar.is_valid:
                updates.append((order, OrderStatus.CANCELLED, 0, "停牌/无行情，订单作废"))
                continue
            fill, status, message = self.match_one(order, bar, date)
            if fill is not None:
                fills.append(fill)
            updates.append((order, status, fill.quantity if fill else 0, message))
        return MatchResult(fills=fills, updates=updates)

    def match_one(self, order: Order, bar: Bar, date) -> Tuple[Optional[Fill], OrderStatus, str]:
        side = order.side
        pre_close = bar.pre_close or bar.close
        limit_up = self.contract.limit_up_price(pre_close, bar.symbol, bar.is_st)
        limit_down = self.contract.limit_down_price(pre_close, bar.symbol, bar.is_st)

        if self.block_limit_move:
            if side is Side.BUY and self.contract.is_limit_up(bar, pre_close):
                return None, OrderStatus.REJECTED, f"涨停封板（涨停价 {limit_up}），买单无法成交"
            if side is Side.SELL and self.contract.is_limit_down(bar, pre_close):
                return None, OrderStatus.REJECTED, f"跌停封板（跌停价 {limit_down}），卖单无法成交"

        base = self.base_price(order, bar)
        if order.order_type.value == "limit" and order.limit_price:
            lp = float(order.limit_price)
            if side is Side.BUY:
                if bar.low > lp + 1e-9:
                    return None, OrderStatus.CANCELLED, f"限价 {lp} 高于当日最低价 {round(bar.low, 3)}，未成交"
                base = min(base, lp)
            else:
                if bar.high < lp - 1e-9:
                    return None, OrderStatus.CANCELLED, f"限价 {lp} 低于当日最高价 {round(bar.high, 3)}，未成交"
                base = max(base, lp)

        slipped = self.cost_model.slippage(base, side, bar=bar, tick_size=self.contract.tick_size)
        price = min(max(slipped, bar.low), bar.high)
        price = min(max(price, limit_down), limit_up)
        price = self.contract.round_to_tick(price)
        if price <= 0:
            return None, OrderStatus.REJECTED, "成交价非正，数据异常"

        # 成交量约束
        lot = self.contract.lot_size
        capacity = int(math.floor(bar.volume * self.max_participation_rate)) if self.max_participation_rate > 0 else order.quantity
        qty = min(int(order.quantity), capacity)
        if qty < order.quantity:
            qty = qty - qty % lot  # 部分成交仍按整手取整
        if qty <= 0:
            return None, OrderStatus.REJECTED, (
                f"当日成交量 {bar.volume:,.0f} 股，参与率上限 {self.max_participation_rate:.0%}，无可成交量"
            )

        gross = qty * price
        breakdown = self.cost_model.costs(order.symbol, side, qty, price)
        slippage_cost = abs(price - base) * qty
        if side is Side.BUY:
            cash_flow = -(gross + breakdown.total)
        else:
            cash_flow = gross - breakdown.total

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=side,
            date=bar.date,
            quantity=qty,
            price=price,
            raw_price=base,
            gross_amount=gross,
            commission=breakdown.commission,
            stamp_tax=breakdown.stamp_tax,
            transfer_fee=breakdown.transfer_fee,
            other_fee=breakdown.other_fee,
            slippage_cost=slippage_cost,
            cash_flow=cash_flow,
            reason=order.reason,
        )
        status = OrderStatus.FILLED if qty == order.quantity else OrderStatus.PARTIAL
        message = "成交" if status is OrderStatus.FILLED else f"部分成交 {qty}/{order.quantity}（受参与率限制）"
        return fill, status, message
