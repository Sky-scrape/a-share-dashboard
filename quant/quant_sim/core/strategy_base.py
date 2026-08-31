"""策略基类与内置策略注册。

两种写法并存，按需选择：

1. **事件驱动（推荐，正式版）**：继承 `Strategy`，实现 `on_bar(context)`。
   ```python
   class Demo(Strategy):
       def on_bar(self, ctx):
           if ctx.date.day % 20 == 1 and not ctx.pending:
               ctx.target_percent("510300", 0.5)
   ```

2. **信号式（研究快速验证）**：实现 `CrossSectionalStrategy.select(ctx)`，
   返回 {标的: 目标权重}，引擎按整手取整下单。
"""

from __future__ import annotations

from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Context

__all__ = ["Strategy", "CrossSectionalStrategy"]


class Strategy:
    """所有策略的基类。子类可选实现 on_start / on_bar / on_finish。"""

    name: str = "strategy"
    params: Dict[str, object] = {}

    def on_start(self, ctx: "Context") -> None:
        """回测开始前调用一次，可做参数校验、预计算。"""

    def on_bar(self, ctx: "Context") -> None:
        """每个交易日调用一次（在该日撮合完成后、下单窗口内）。"""
        raise NotImplementedError

    def on_finish(self, ctx: "Context") -> None:
        """回测结束前调用一次（此时还未强制平仓）。"""


class CrossSectionalStrategy(Strategy):
    """横截面选币/选股策略基类：只需给出目标权重，撮合与调仓由引擎完成。"""

    #: 调仓间隔（交易日），1 表示每日调仓
    rebalance_days: int = 1
    #: 权重和超过该值时按比例缩放（防止爆仓）
    max_total_weight: float = 1.0

    def select(self, ctx: "Context") -> Dict[str, float]:
        raise NotImplementedError

    def on_bar(self, ctx: "Context") -> None:
        if not ctx.should_rebalance(self.rebalance_days):
            return
        weights = self.select(ctx)
        total = sum(abs(w) for w in weights.values())
        if total > self.max_total_weight and total > 0:
            scale = self.max_total_weight / total
            weights = {k: v * scale for k, v in weights.items()}
        ctx.rebalance(weights, reason=self.name)
