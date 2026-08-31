"""交易成本模型：佣金、印花税、过户费、规费、滑点。

A 股现行（2023-08 之后）常见口径：
  * 佣金：万 2.5（可谈至万 1），双边，单笔最低 5 元
  * 印花税：千 0.5，仅卖出
  * 过户费：沪深双边万 0.1（0.001%）
  * 其他规费（证管费+经手费）：约万 0.1，双边，通常已含在佣金里，默认 0
场内基金（ETF）：免印花税、免过户费，佣金按券商约定（多数无 5 元最低）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .contract import Contract
from .types import Side

__all__ = ["CostModel", "CostBreakdown"]


@dataclass(frozen=True)
class CostBreakdown:
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    other_fee: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee + self.other_fee


@dataclass
class CostModel:
    buy_commission_rate: float = 2.5e-4
    sell_commission_rate: float = 2.5e-4
    min_commission: float = 5.0
    stamp_tax_rate_sell: float = 5e-4
    transfer_fee_rate: float = 1e-5
    other_fee_rate: float = 0.0
    #: ETF 免印花税/过户费；无最低佣金
    fund_commission_rate: float = 2.5e-4
    fund_min_commission: float = 0.0
    #: 滑点：none | percent | tick | spread
    slippage_model: str = "spread"
    slippage_value: float = 5e-4
    #: spread 模型下，买卖各承担的价差比例（0.5 表示各承担半个买卖价差）
    spread_share: float = 0.5

    def is_fund(self, symbol: str) -> bool:
        return Contract.is_fund(symbol)

    def commission_rate(self, symbol: str, side: Side) -> float:
        if self.is_fund(symbol):
            return self.fund_commission_rate
        return self.buy_commission_rate if side is Side.BUY else self.sell_commission_rate

    def min_commission_for(self, symbol: str) -> float:
        return self.fund_min_commission if self.is_fund(symbol) else self.min_commission

    def costs(self, symbol: str, side: Side, quantity: int, price: float) -> CostBreakdown:
        """按成交价（含滑点）计算各项费用。成交额 = 价格 × 股数。"""
        amount = float(quantity) * float(price)
        if amount <= 0:
            return CostBreakdown()
        commission = max(amount * self.commission_rate(symbol, side), self.min_commission_for(symbol))
        stamp = 0.0 if (side is Side.BUY or self.is_fund(symbol)) else amount * self.stamp_tax_rate_sell
        transfer = 0.0 if self.is_fund(symbol) else amount * self.transfer_fee_rate * 2.0  # 沪深双边
        other = amount * self.other_fee_rate
        return CostBreakdown(
            commission=round(commission, 2),
            stamp_tax=round(stamp, 2),
            transfer_fee=round(transfer, 4),
            other_fee=round(other, 4),
        )

    def estimated_cost_pct(self, symbol: str, side: Side) -> float:
        """不含最低佣金的费率估计（用于下单前的资金预留与风控）。"""
        rate = self.commission_rate(symbol, side)
        if not self.is_fund(symbol):
            rate += self.transfer_fee_rate * 2.0 + self.other_fee_rate
            if side is Side.SELL:
                rate += self.stamp_tax_rate_sell
        return rate

    def slippage(
        self,
        price: float,
        side: Side,
        bar=None,
        tick_size: float = 0.01,
    ) -> float:
        """返回加了滑点后的成交价（买高卖低），不做区间裁剪（由撮合层负责）。"""
        model = (self.slippage_model or "none").lower()
        direction = 1 if side is Side.BUY else -1
        if model == "none" or price <= 0:
            return price
        if model == "percent":
            return price * (1.0 + direction * self.slippage_value)
        if model == "tick":
            return price + direction * self.slippage_value * tick_size
        if model == "spread":
            if bar is None or getattr(bar, "low", 0) <= 0:
                return price * (1.0 + direction * self.slippage_value)
            half_spread = (bar.high - bar.low) * self.spread_share
            return price + direction * min(half_spread, price * self.slippage_value)
        raise ValueError(f"未知滑点模型: {self.slippage_model}")

    @classmethod
    def from_config(cls, cost, slippage) -> "CostModel":
        return cls(
            buy_commission_rate=cost.buy_rate,
            sell_commission_rate=cost.sell_rate,
            min_commission=cost.min_commission,
            stamp_tax_rate_sell=cost.stamp_tax_rate_sell,
            transfer_fee_rate=cost.transfer_fee_rate,
            other_fee_rate=cost.other_fee_rate,
            fund_commission_rate=cost.fund_rate,
            fund_min_commission=cost.fund_min_commission,
            slippage_model=slippage.model,
            slippage_value=slippage.value,
            spread_share=slippage.spread_share,
        )
