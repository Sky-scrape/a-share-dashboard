"""声明式规则策略 + 在线代码沙箱测试。"""

from __future__ import annotations

import pytest

from quant_sim.core.config import BacktestConfig
from quant_sim.core.engine import run_backtest
from quant_sim.data.sample import make_demo_panel
from quant_sim.strategies.rule_based import (
    GenericRuleStrategy,
    IND,
    cross,
    generate_python_code,
    last_val,
)
from quant_sim.strategies.sandbox import TEMPLATES, StrategyCodeError, build_user_strategy


@pytest.fixture(scope="module")
def panel():
    return make_demo_panel(seed=7)


@pytest.fixture(scope="module")
def cfg():
    return BacktestConfig(start_date="2023-01-01", end_date="2025-12-31", benchmark="510300")


def ind(i, c="", **p):
    return {"t": "ind", "i": i, "p": p, "c": c}


def fld(f):
    return {"t": "field", "f": f}


def const(v):
    return {"t": "const", "v": float(v)}


SPECS = [
    # 单标的均线金叉 + 常数条件 + 纯止盈
    {
        "symbols": ["510300"],
        "entry": {"mode": "all", "conds": [{"left": ind("sma", n=20), "op": "cross_above", "right": ind("sma", n=60)}]},
        "exit": {"mode": "any", "conds": [{"left": fld("close"), "op": "gt", "right": const(0)}]},
        "target_weight": 0.95,
        "take_profit_pct": 0.5,
    },
    # 多标的 + 复合入场(且) + 复合出场(或) + 止损止盈移动止损全家桶
    {
        "symbols": ["510300", "600519"],
        "entry": {
            "mode": "all",
            "conds": [
                {"left": ind("sma", n=20), "op": "cross_above", "right": ind("sma", n=60)},
                {"left": ind("rsi", n=14), "op": "gt", "right": const(30)},
            ],
        },
        "exit": {
            "mode": "any",
            "conds": [
                {"left": fld("close"), "op": "lt", "right": ind("boll", "lower", n=20, k=2.0)},
                {"left": ind("rsi", n=14), "op": "gt", "right": const(70)},
            ],
        },
        "target_weight": 0.45,
        "stop_loss_pct": 0.08,
        "take_profit_pct": 0.30,
        "trailing_pct": 0.15,
    },
    # 唐奇安突破 + MACD 死叉出场 + 单风控
    {
        "symbols": ["600519"],
        "entry": {"mode": "any", "conds": [{"left": fld("close"), "op": "gt", "right": ind("donchian", "upper", n=20)}]},
        "exit": {"mode": "all", "conds": [{"left": ind("macd", "hist", fast=12, slow=26, signal=9), "op": "cross_below", "right": const(0)}]},
        "target_weight": 0.5,
        "stop_loss_pct": 0.1,
    },
    # 空规则（只有风控）不应报错
    {"symbols": ["510300"], "entry": None, "exit": None, "target_weight": 0.6, "take_profit_pct": 0.05},
    # 量比/ATR/EMA/动量指标都实例化一遍
    {
        "symbols": ["510300"],
        "entry": {
            "mode": "all",
            "conds": [
                {"left": ind("vol_ratio", n=5), "op": "gt", "right": const(1.2)},
                {"left": ind("mom", n=20), "op": "gt", "right": const(0)},
            ],
        },
        "exit": {"mode": "any", "conds": [{"left": fld("close"), "op": "lt", "right": ind("ema", n=10)}]},
        "target_weight": 0.4,
    },
]


def order_key(result):
    if len(result.orders) == 0:
        return []
    cols = ["created_date", "symbol", "side", "status", "quantity"]
    return sorted(map(tuple, result.orders[cols].astype(str).values.tolist()))


@pytest.mark.parametrize("spec", SPECS, ids=[f"spec{i}" for i in range(len(SPECS))])
def test_rule_engine_matches_exported_code(panel, cfg, spec):
    """表单规则引擎与导出的等价 Python 必须逐单一致。

    导出代码经沙盒正式 API（build_user_strategy，含 AST 白名单扫描）加载，
    同时覆盖「导出产物可用」与「沙盒接受导出代码」两条链路。"""
    r1 = run_backtest(GenericRuleStrategy(**spec), panel, cfg)
    # 导出产物 import 引擎本体（from quant_sim.core.strategy_base import Strategy），
    # 在线沙盒白名单只放行三方库——测试经 extra_modules 构造期参数临时放行
    # quant_sim 顶级包（白名单是 frozenset，生产口径不受运行期污染）。
    strategy = build_user_strategy(generate_python_code(spec), extra_modules={"quant_sim"})
    r2 = run_backtest(strategy, panel, cfg)
    assert order_key(r1) == order_key(r2)
    assert abs(r1.metrics["累计收益率"] - r2.metrics["累计收益率"]) < 1e-12
    assert r1.metrics["交易次数"] == r2.metrics["交易次数"]


def test_cross_and_gt_semantics(panel, cfg):
    """金叉策略必须真的产生交易；cross 工具函数语义正确。"""
    import pandas as pd

    s = pd.Series([1.0, 2.0, 3.0])
    b = pd.Series([2.0, 2.0, 2.0])
    assert cross(s, b, above=True)
    assert not cross(b, s, above=True)
    assert last_val(s) == 3.0
    spec = SPECS[0] | {"symbols": ["510300", "600519"]}
    r = run_backtest(GenericRuleStrategy(**spec), panel, cfg)
    assert r.metrics["交易次数"] > 0


def test_stops_trigger_reasons(panel, cfg):
    """必跌行情 + 5% 止损：成交原因里应出现风控。"""
    spec = {
        "symbols": ["600519"],
        "entry": {"mode": "all", "conds": [{"left": fld("open"), "op": "gt", "right": const(-1)}]},  # 恒真（open>−1）
        "exit": None,
        "target_weight": 0.5,
        "stop_loss_pct": 0.05,
    }
    r = run_backtest(GenericRuleStrategy(**spec), panel, cfg)
    reasons = " ".join(map(str, r.orders["reason"].tolist()))
    assert "风控" in reasons or "止损" in reasons, reasons[:200]


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_templates_run(panel, cfg, name):
    strat = build_user_strategy(TEMPLATES[name])
    r = run_backtest(strat, panel, cfg)
    assert r.metrics["累计收益率"] == r.metrics["累计收益率"]  # 非 NaN


def test_sandbox_blocks_dangerous_code():
    for bad in [
        "import os\nprint(os.getcwd())",
        "import subprocess",
        "open('/etc/passwd')",
        "exec('x=1')",
        "x = 1",                      # 没有策略类
        "def f(:",                    # 语法错误
    ]:
        with pytest.raises(StrategyCodeError):
            build_user_strategy(bad)


def test_sandbox_allows_whitelist_import():
    strat = build_user_strategy(
        "import math\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n"
    )
    assert strat is not None


def test_sandbox_blocks_escape_chains():
    """已知逃逸链回归：__builtins__/__import__/__loader__/importlib/dunder 反射。
    每条都必须被 AST 扫描或受限 import 拦下，报 StrategyCodeError（带定位）。"""
    escapes = [
        # __builtins__ 引用（拿到真 builtins 字典即出笼）
        "x = __builtins__\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        # __import__ 直呼（绕过命名空间里替换过的 __import__ 名）
        "os = __import__('os')\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        # __loader__/__spec__ 模块自省链
        "x = __loader__\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        "x = __spec__\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        # importlib 间接 import（白名单外模块）
        "import importlib\nos = importlib.import_module('os')\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        # dunder 反射经典链：().__class__.__mro__[1].__subclasses__()
        "x = ().__class__.__mro__[1].__subclasses__()\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
        # getattr 间接取 dunder 属性（AST 属性节点同样命中黑名单）
        "x = getattr(dict, '__bases__')\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n",
    ]
    for bad in escapes:
        with pytest.raises(StrategyCodeError):
            build_user_strategy(bad)


def test_sandbox_whitelist_immutable():
    """白名单必须是 frozenset：运行期 .add 必须失败（防止测试/业务代码顺手变异全局）。"""
    from quant_sim.strategies import sandbox as sb

    assert isinstance(sb._ALLOWED_MODULES, frozenset)
    with pytest.raises(AttributeError):
        sb._ALLOWED_MODULES.add("os")
    # 白名单外模块即使在 extra_modules 之外的调用中依然被拒
    with pytest.raises(StrategyCodeError):
        build_user_strategy("import os\n\nclass MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n")


def test_generated_code_public_helpers():
    """导出代码只引用公开符号（IND/last_val/cross），不碰私有名。"""
    code = generate_python_code(SPECS[1])
    assert "_last(" not in code and "_cross(" not in code
    assert "last_val" in code and "cross" in code and "IND." in code
