"""指标显示口径的**唯一实现**（display_value）+ CLI 摘要文本。

背景（8/30 设计评审）：同一指标"怎么显示"以前写了三遍——
performance._display（按子串判百分比）、report/formatter.format_summary（逐 key 手写）、
report/exporter._card_value（另一套阈值规则）——三处规则已经漂移（如"率 in key"vs
"收益率/回撤/率…"子串表），新加指标要改三处。现在只有这里一份规则，
performance 与 exporter 都调用它。
"""

from __future__ import annotations

import math
from typing import Dict

__all__ = ["PERCENT_KEYS", "is_percent_key", "display_value", "format_summary"]

#: 视为百分比的子串（唯一来源）
PERCENT_KEYS = (
    "收益率", "回撤", "率", "占比", "胜率", "换手", "超额", "跟踪误差", "费用占初始资金比",
)
_COUNT_KEYS = ("交易日数", "交易次数", "最长回撤天数", "平均持仓天数")
_RATIO_KEYS = ("夏普", "索提诺", "卡玛", "信息比率", "Beta", "相关性", "盈亏比")
_MONEY_KEYS = (
    "初始资金", "期末权益", "总手续费", "其中佣金", "其中印花税", "其中过户费",
    "滑点成本", "单笔最大盈利", "单笔最大亏损", "总盈利", "总亏损",
)


def is_percent_key(key: str) -> bool:
    return any(k in key for k in PERCENT_KEYS)


def display_value(key: str, value, *, money_unit: bool = True) -> str:
    """指标 → 展示字符串（表格/卡片/摘要共用）。

    money_unit=False 时金额不带「元」字（HTML 卡片小空间用），数字位数规则不变。
    """
    if isinstance(value, str):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, float) and math.isinf(value):
        return "∞"
    if isinstance(value, int) and key not in _COUNT_KEYS:
        return f"{value:,}"
    if key in _COUNT_KEYS:
        return f"{value:,.0f}"
    if any(k in key for k in _RATIO_KEYS):
        return f"{value:.3f}"
    if any(k in key for k in _MONEY_KEYS):
        return (f"{value:,.2f} 元" if money_unit else f"{value:,.2f}")
    if is_percent_key(key):
        return f"{value:.2%}"
    return f"{value:,.4f}"


def format_summary(metrics: Dict[str, float], width: int = 46) -> str:
    lines = ["=" * width, "回测绩效摘要", "=" * width]
    rows = [
        "回测区间", "累计收益率", "年化收益率", "年化波动率", "最大回撤",
        "夏普比率", "索提诺比率", "卡玛比率", "交易次数", "胜率", "盈亏比",
        "平均持仓天数", "总手续费", "滑点成本", "期末权益",
    ]
    if "基准年化收益" in metrics:
        rows += ["基准年化收益", "年化超额收益", "信息比率", "Beta"]
    key_width = max(len(k) for k in rows)
    for k in rows:
        v = display_value(k, metrics.get(k, "-"))
        lines.append(f"{k:<{key_width}}  {v}")
    lines.append("=" * width)
    return "\n".join(lines)
