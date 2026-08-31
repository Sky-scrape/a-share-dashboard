"""模板策略：复制这个文件改成你的策略。

运行：  python examples/my_strategy.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant_sim import BacktestConfig, run_backtest
from quant_sim.core.strategy_base import Strategy
from quant_sim.data.sample import make_demo_panel


class MyStrategy(Strategy):
    """RSI 超卖抄底 + 固定止盈（示例，仅演示 API 用法）。"""

    name = "my_strategy"

    def __init__(self, symbol: str = "510300", rsi_window: int = 14, low: float = 30, high: float = 65, weight: float = 0.5):
        self.symbol = symbol
        self.n = rsi_window
        self.low = low
        self.high = high
        self.weight = weight

    def rsi(self, ctx) -> float | None:
        df = ctx.history(self.symbol, self.n * 4)   # 截至昨日（防未来函数）
        if df is None or len(df) < self.n + 1:
            return None
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(self.n).mean()
        loss = (-delta.clip(upper=0)).rolling(self.n).mean()
        rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else float("inf")
        return 100 - 100 / (1 + rs)

    def on_bar(self, ctx):
        if ctx.pending:
            return
        r = self.rsi(ctx)
        if r is None:
            return
        holding = ctx.holding(self.symbol)
        if holding == 0 and r < self.low:
            ctx.target_percent(self.symbol, self.weight, reason=f"RSI {r:.0f} 超卖")
        elif holding > 0 and r > self.high:
            ctx.sell(self.symbol, quantity=holding, reason=f"RSI {r:.0f} 止盈")


if __name__ == "__main__":
    panel = make_demo_panel()
    cfg = BacktestConfig(
        start_date="2023-01-01",
        end_date="2025-12-31",
        initial_cash=1_000_000,
        benchmark="510300",
    )
    result = run_backtest(MyStrategy(), panel, cfg)
    print(result.summary())
    print("报告：", result.save("results", "my_strategy"))
