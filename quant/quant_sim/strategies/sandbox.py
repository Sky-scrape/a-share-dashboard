"""在线代码策略沙箱：在受限命名空间 exec 用户源码，找出 Strategy 子类。

安全边界（本地单用户工具，按"防误不防恶"设计）：
- 替换 __import__：只放行白名单模块（numpy/pandas/math/statistics/random/datetime/itertools/functools）
- 不给 open/exec/eval/compile/__builtins__ 引用，不改全局
- AST 静态扫描：拒绝 dunder 属性逃逸链（`__class__.__mro__[1].__subclasses__()` 等）
- ⚠️ **没有 exec 超时**：旧注释说「由 Streamlit 前端刷新兜底」是错的——类体里的
  `while True` 会占死 ScriptRunner 线程，该会话无法再 rerun，只能重启进程。
- ⚠️ 不要把不信任的存档 JSON 拷进 strategies_store/——kind=code 存档在回测/出信号时
  会重新 exec（分享 = 分享可执行代码）。执行前需显式 allow_exec 确认。
"""

from __future__ import annotations

import ast
import builtins as _builtins
import traceback
from typing import Dict, Optional, Type

import numpy as np
import pandas as pd

from ..core.strategy_base import CrossSectionalStrategy, Strategy

_ALLOWED_MODULES = {
    "numpy", "pandas", "math", "statistics", "random", "datetime",
    "itertools", "functools", "collections", "dataclasses", "typing",
    # time 已移除：time.sleep(1e9) 在无超时实现下会挂死会话（防误角度也不该给）
}

#: dunder 属性逃逸链黑名单：拿到这些就能越出受限 builtins（CPython 经典沙箱逃逸路径）。
_BANNED_ATTRS = {
    "__class__", "__mro__", "__subclasses__", "__bases__", "__base__", "__globals__",
    "__dict__", "__code__", "__closure__", "__getattribute__", "__reduce__",
    "__reduce_ex__", "__builtins__", "__import__", "__loader__", "__spec__",
}

_SAFE_BUILTINS: Dict[str, object] = {
    k: getattr(_builtins, k)
    for k in (
        "__build_class__",  # class 语句需要
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "format",
        "frozenset", "int", "isinstance", "len", "list", "map", "max", "min", "print",
        "range", "repr", "reversed", "round", "set", "sorted", "str", "sum", "super", "tuple",
        "zip", "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "ZeroDivisionError", "StopIteration", "RuntimeError", "AttributeError", "NotImplementedError",
    )
    if hasattr(_builtins, k)
}


def _guarded_import(name: str, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
    root = name.split(".")[0]
    if root not in _ALLOWED_MODULES:
        raise ImportError(f"在线策略只允许 import {sorted(_ALLOWED_MODULES)}，拒绝 `{name}`")
    import importlib

    return importlib.import_module(name)


class StrategyCodeError(Exception):
    """用户代码错误（带定位信息）。"""


def make_namespace() -> Dict[str, object]:
    ns: Dict[str, object] = {
        "__builtins__": {**_SAFE_BUILTINS, "__import__": _guarded_import},
        "__name__": "user_strategy",  # __build_class__ 建类时需要
        "__doc__": None,
        "pd": pd,
        "np": np,
        "Strategy": Strategy,
        "CrossSectionalStrategy": CrossSectionalStrategy,
    }
    return ns


def scan_user_code(code: str) -> None:
    """exec 前的 AST 静态扫描：拒绝已知沙箱逃逸写法。

    定位是「把最大攻击面从信任文件名升级为过闸」，不是完备安全（防误不防恶的边界不变）；
    命中抛 StrategyCodeError 并带行号。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return  # 语法错误交给 compile 阶段报带行号的友好信息
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _BANNED_ATTRS:
            raise StrategyCodeError(
                f"❌ 你的代码 第 {node.lineno} 行：禁止访问 {node.attr}（沙箱逃逸链）。"
                "策略代码不需要反射内置对象；确有正常需求请转本地 Python 脚本跑。"
            )
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            raise StrategyCodeError(f"❌ 你的代码 第 {node.lineno} 行：禁止引用 __builtins__。")


def compile_user_strategy(code: str) -> Type[Strategy]:
    """exec 用户代码，返回其中定义的最后一个 Strategy 子类（约定：主策略放最后，或命名 MyStrategy）。"""
    if not code.strip():
        raise StrategyCodeError("代码为空")
    scan_user_code(code)
    ns = make_namespace()
    before = set()
    try:
        exec(compile(code, "<我的策略>", "exec"), ns)  # noqa: S102
    except Exception:
        tb = traceback.format_exc()
        raise StrategyCodeError(tb.replace('File "<我的策略>"', "❌ 你的代码")) from None
    found = [
        v for v in ns.values()
        if isinstance(v, type) and issubclass(v, Strategy) and v not in (Strategy, CrossSectionalStrategy)
    ]
    if not found:
        raise StrategyCodeError("代码里没有定义 Strategy 子类。请写：\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx): ...")
    # 命名优先 MyStrategy，否则取源码里最后定义的
    named = [c for c in found if c.__name__ == "MyStrategy"]
    chosen = named[-1] if named else found[-1]
    _ = before
    return chosen


def build_user_strategy(code: str, **params) -> Strategy:
    cls = compile_user_strategy(code)
    try:
        return cls(**params)
    except TypeError as e:
        raise StrategyCodeError(f"实例化 {cls.__name__} 失败：{e}\n（检查 __init__ 参数名与默认值）") from None


# ------------------------------------------------------------------ 模板
TIMING_TEMPLATE = '''"""我的策略：RSI 抄底 + 趋势过滤（示例，直接改这里的数字就是"设定参数"）"""

class MyStrategy(Strategy):
    name = "我的RSI策略"

    SYMBOL = "510300"     # 交易标的（改代码里的常量即可）
    RSI_N = 14            # RSI 窗口
    BUY_BELOW = 32        # RSI 低于多少买入
    SELL_ABOVE = 62       # RSI 高于多少卖出
    TREND_N = 120         # 趋势过滤：收盘价站上 N 日均线才允许买入
    WEIGHT = 0.95         # 买入仓位（占总权益）

    def on_bar(self, ctx):
        df = ctx.history(self.SYMBOL, max(self.RSI_N * 4, self.TREND_N + 5))
        if df is None or len(df) < self.TREND_N + 2:
            return
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.RSI_N).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).rolling(self.RSI_N).mean().iloc[-1]
        rsi = 100 - 100 / (1 + gain / loss) if loss else 100.0
        trend_ok = close.iloc[-1] > close.rolling(self.TREND_N).mean().iloc[-1]
        holding = ctx.holding(self.SYMBOL)

        if holding == 0 and rsi < self.BUY_BELOW and trend_ok:
            ctx.target_percent(self.SYMBOL, self.WEIGHT, reason=f"RSI {rsi:.0f} 超卖且趋势向上")
        elif holding > 0 and rsi > self.SELL_ABOVE:
            ctx.sell(self.SYMBOL, quantity=holding, reason=f"RSI {rsi:.0f} 止盈")
'''

ROTATION_TEMPLATE = '''"""我的策略：横截面动量轮动（只需写 select 函数，返回 {标的: 目标权重}）"""

class MyStrategy(CrossSectionalStrategy):
    name = "我的轮动"

    UNIVERSE = ["510300", "510500", "159915", "512880", "588000"]
    LOOKBACK = 60    # 动量回看天数
    TOP_N = 2        # 持有动量最强的 N 只
    REBALANCE = 5    # 每 5 个交易日调一次仓
    MIN_MOM = 0.0    # 绝对动量过滤：低于此收益的不买（0=负动量空仓）

    rebalance_days = REBALANCE

    def select(self, ctx):
        scores = {}
        for s in self.UNIVERSE:
            df = ctx.history(s, self.LOOKBACK + 2)
            if df is None or len(df) < self.LOOKBACK + 1:
                continue
            mom = df["close"].iloc[-1] / df["close"].iloc[-self.LOOKBACK - 1] - 1
            if mom >= self.MIN_MOM:
                scores[s] = mom
        ranked = sorted(scores, key=scores.get, reverse=True)[: self.TOP_N]
        return {s: 1.0 / len(ranked) for s in ranked} if ranked else {}
'''

MINIMAL_TEMPLATE = '''"""最小骨架：把你的逻辑写进 on_bar。

ctx 常用 API：
  ctx.history(代码, N)      截至昨收的 N 根日 bar（DataFrame: open/high/low/close/volume/amount/pre_close）
  ctx.price(代码)           当日收盘/开盘等（price(s, "open")）
  ctx.holding(代码)         持仓股数；ctx.position(代码).avg_cost 成本价
  ctx.cash / ctx.equity     现金 / 总权益
  ctx.universe              当日有行情的标的列表
  ctx.target_percent(s, w)  把 s 调到占总权益 w；ctx.buy/ctx.sell 定量
  ctx.rebalance({s: w})     一键按权重调仓；ctx.flat() 全清仓
  ctx.pending               未成交挂单（防重复下单）
"""

class MyStrategy(Strategy):
    name = "我的策略"

    def on_bar(self, ctx):
        # 示例：每个标的收盘 20 日新高买入、跌破 10 日线卖出
        for s in ctx.universe:
            df = ctx.history(s, 25)
            if df is None or len(df) < 21:
                continue
            high20 = df["high"].iloc[:-1].rolling(20).max().iloc[-1]
            holding = ctx.holding(s)
            if holding == 0 and df["close"].iloc[-1] >= high20 and not ctx.pending:
                ctx.target_percent(s, 0.30, reason="20日新高突破")
            elif holding > 0 and df["close"].iloc[-1] < df["close"].rolling(10).mean().iloc[-1]:
                ctx.sell(s, quantity=holding, reason="跌破10日线")
'''

TEMPLATES: Dict[str, str] = {
    "RSI 择时（单标的）": TIMING_TEMPLATE,
    "动量轮动（多标的）": ROTATION_TEMPLATE,
    "最小骨架（海龟式突破）": MINIMAL_TEMPLATE,
}
