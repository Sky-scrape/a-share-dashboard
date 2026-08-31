"""布林带均值回归策略（单标的网格/回归类范式示例）。

规则：
  * 收盘价跌破下轨 → 分批买入 1 份（最多 max_batches 份）
  * 收盘价回到中轨之上 → 全部卖出
  * 用布林带宽动态分配仓位，避免单边下跌中一次性打满

⚠️ 均值回归在 A 股个股上风险极高（可能遇到退市/长熊），示例主要用于
   流动性好的宽基 ETF；策略里默认启用现金分批与最大批次限制。
"""

from __future__ import annotations

from typing import List

import pandas as pd

from ..core.engine import Context
from ..core.strategy_base import Strategy

__all__ = ["MeanReversionStrategy"]


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(
        self,
        symbol: str,
        window: int = 20,
        num_std: float = 2.0,
        max_batches: int = 4,
        weight_per_batch: float = 0.24,
        exit_to_mid: bool = True,
    ) -> None:
        self.symbol = symbol
        self.window = window
        self.num_std = num_std
        self.max_batches = max_batches
        self.weight_per_batch = weight_per_batch
        self.exit_to_mid = exit_to_mid
        self.batches_held = 0

    def bands(self, ctx: Context):
        df = ctx.history(self.symbol, self.window + 5)
        if df is None or len(df) < self.window:
            return None
        close = df["close"]
        mid = close.rolling(self.window).mean().iloc[-1]
        std = close.rolling(self.window).std(ddof=0).iloc[-1]
        if pd.isna(mid) or pd.isna(std):
            return None
        return float(mid), float(mid + self.num_std * std), float(mid - self.num_std * std)

    def on_bar(self, ctx: Context) -> None:
        if ctx.pending:
            return
        bands = self.bands(ctx)
        if bands is None:
            return
        mid, upper, lower = bands
        price = ctx.price(self.symbol)
        if price is None:
            return
        holding = ctx.holding(self.symbol)
        if holding == 0:
            self.batches_held = 0
        if price < lower and self.batches_held < self.max_batches:
            ctx.target_percent(self.symbol, (self.batches_held + 1) * self.weight_per_batch, reason="跌破下轨加仓")
            self.batches_held += 1
        elif holding > 0 and self.exit_to_mid and price > mid:
            ctx.sell(self.symbol, quantity=holding, reason="回到中轨离场")
            self.batches_held = 0
