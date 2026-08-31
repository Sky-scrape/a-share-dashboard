"""双均线趋势策略（单标的，事件驱动范式的入门示例）。

规则：
  * fast MA 上穿 slow MA → 目标仓位 target_weight
  * fast MA 下穿 slow MA → 清仓
  * 信号基于**截至昨日**的收盘价序列（ctx.history 默认不含当日），次日开盘成交

可选风控：ATR 移动止损。
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.engine import Context
from ..core.strategy_base import Strategy

__all__ = ["DualMAStrategy"]


def _ma_cross_signal(df: pd.DataFrame, fast: int, slow: int) -> Optional[int]:
    """返回 1（金叉当日）/ -1（死叉当日）/ 0（无交叉）/ None（数据不足）。"""
    if df is None or len(df) < slow + 2:
        return None
    close = df["close"]
    ma_f = close.rolling(fast).mean()
    ma_s = close.rolling(slow).mean()
    above = (ma_f > ma_s).astype(int)
    if above.iloc[-1] == 1 and above.iloc[-2] == 0:
        return 1
    if above.iloc[-1] == 0 and above.iloc[-2] == 1:
        return -1
    return 0


class DualMAStrategy(Strategy):
    name = "dual_ma"

    def __init__(
        self,
        symbol: str,
        fast: int = 20,
        slow: int = 60,
        target_weight: float = 0.95,
        atr_stop: Optional[float] = None,   # 例如 3.0 = 3 倍 ATR 移动止损
        atr_window: int = 14,
    ) -> None:
        assert fast < slow, "fast 必须小于 slow"
        self.symbol = symbol
        self.fast = fast
        self.slow = slow
        self.target_weight = target_weight
        self.atr_stop = atr_stop
        self.atr_window = atr_window
        self._highest_close = 0.0

    def on_bar(self, ctx: Context) -> None:
        if ctx.pending:
            return
        df = ctx.history(self.symbol, self.slow + 40)
        sig = _ma_cross_signal(df, self.fast, self.slow)
        holding = ctx.holding(self.symbol) > 0
        if sig is None:
            return

        if holding:
            close = ctx.price(self.symbol) or float("nan")
            self._highest_close = max(self._highest_close, close)
            # ATR 移动止损
            if self.atr_stop and df is not None and len(df) > self.atr_window:
                tr = (df["high"] - df["low"]).rolling(self.atr_window).mean().iloc[-1]
                if pd.notna(tr) and close < self._highest_close - self.atr_stop * tr:
                    ctx.sell(self.symbol, quantity=ctx.holding(self.symbol), reason="ATR 止损")
                    self._highest_close = 0.0
                    return
            if sig == -1:
                ctx.sell(self.symbol, quantity=ctx.holding(self.symbol), reason="死叉离场")
                self._highest_close = 0.0
        elif sig == 1:
            self._highest_close = ctx.price(self.symbol) or 0.0
            ctx.target_percent(self.symbol, self.target_weight, reason="金叉入场")
