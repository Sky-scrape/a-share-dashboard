"""A 股 / 场内 ETF 日线量化模拟平台（回测引擎 MVP）。

对外主要入口：
    from quant_sim import BacktestConfig, BacktestEngine, run_backtest
"""

from __future__ import annotations

import sys as _sys

# Windows GBK 控制台兼容：CLI 输出带 emoji/警告符号时，不可编码字符以 ? 代替而非直接抛
# UnicodeEncodeError（与 hithink._cli 的 subprocess encoding 修复同一类坑）。
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # 已被捕获重定向、不支持 reconfigure 的流（pytest/IDE）静默跳过
        pass

from .core.config import BacktestConfig, CostConfig, RiskConfig, SlippageConfig
from .core.engine import BacktestEngine, BacktestResult, run_backtest
from .core.types import Bar, BarPanel, Fill, Order, Position, Side, Trade

__version__ = "0.1.0"

__all__ = [
    "BacktestConfig",
    "CostConfig",
    "RiskConfig",
    "SlippageConfig",
    "BacktestEngine",
    "BacktestResult",
    "run_backtest",
    "Bar",
    "BarPanel",
    "Fill",
    "Order",
    "Position",
    "Side",
    "Trade",
    "__version__",
]
