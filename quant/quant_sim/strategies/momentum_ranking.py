"""横截面动量轮动策略（组合调仓范式示例）。

规则（月度调仓）：
  * 用截至上月的 close[-N]/close - 1 计算各标的过去 N 日动量
  * 做多动量最强的 top_n 只，等权；剔除停牌与数据不足者
  * 可选绝对动量过滤：动量 < 0 的持仓清掉，现金等待（规避系统性熊市）

这是 A 股个人量化最常见的“ETF 轮动”骨架。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from ..core.engine import Context
from ..core.strategy_base import Strategy

__all__ = ["MomentumRankingStrategy"]


class MomentumRankingStrategy(Strategy):
    name = "momentum_ranking"

    def __init__(
        self,
        universe: List[str],
        lookback: int = 60,
        top_n: int = 2,
        monthly: bool = True,
        abs_momentum: bool = True,
        weight_per_slot: float = 0.48,
        min_history: Optional[int] = None,
        skip_recent: int = 0,          # 跳过最近 N 日（规避短期反转）
    ) -> None:
        self.universe = list(universe)
        self.lookback = lookback
        self.top_n = top_n
        self.monthly = monthly
        self.abs_momentum = abs_momentum
        self.weight_per_slot = weight_per_slot
        self.min_history = min_history or lookback + 5
        self.skip_recent = skip_recent
        self._last_month = None

    # ------------------------------------------------------------------
    def momentum(self, ctx: Context, symbol: str) -> Optional[float]:
        length = self.lookback + self.skip_recent + 5
        df = ctx.history(symbol, length)
        if df is None or len(df) < self.min_history + self.skip_recent:
            return None
        close = df["close"]
        end = len(close) - 1 - self.skip_recent
        if end - self.lookback < 0:
            return None
        base = float(close.iloc[end - self.lookback])
        now = float(close.iloc[end])
        if base <= 0:
            return None
        return now / base - 1.0

    def should_rebalance(self, ctx: Context) -> bool:
        if not self.monthly:
            return True
        key = (ctx.date.year, ctx.date.month)
        if key == self._last_month:
            return False
        self._last_month = key
        return True

    def on_bar(self, ctx: Context) -> None:
        if ctx.pending or not self.should_rebalance(ctx):
            return
        scores: Dict[str, float] = {}
        for symbol in self.universe:
            bar = ctx.bar(symbol)
            if bar is None or not bar.is_valid:
                continue
            m = self.momentum(ctx, symbol)
            if m is not None:
                scores[symbol] = m
        if not scores:
            return
        ranked: List[str] = sorted(scores, key=lambda s: scores[s], reverse=True)
        picks = ranked[: self.top_n]
        if self.abs_momentum:
            picks = [s for s in picks if scores[s] > 0]
        targets = {s: self.weight_per_slot for s in picks}
        ctx.rebalance(targets, reason=self.name)
