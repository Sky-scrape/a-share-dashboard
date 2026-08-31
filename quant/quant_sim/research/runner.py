"""统一变体运行器：网格/WF 内层/多策略对比/参数邻域/成本压测共用一条「跑批 → 取指标 → 错误行 → 排名」路径。

背景（8/29 设计评审）：同样的循环此前写了四遍——
  * grid_search 自己循环 + 拼 10 个中文指标列 + `_error` 行 + `_score` 排名；
  * compare_strategies 独立再写一遍，错误处理却变成 print 到 stdout；
  * 网页「参数邻域」「成本压测」在 UI 里第三/第四遍手写「扰动 → run_backtest → 取指标 → 建行」。
同一研究在不同入口的指标列、错误语义、配置构造互相漂移。本模块是唯一实现：
  * `run_variants`：变体列表 + 构造器 → DataFrame + 结果对象；错误行不中断批跑；
  * `stress_config`：成本压测（费率/滑点整体放大）的配置派生；
  * `make_backtest_config`：从扁平选项构造 BacktestConfig（UI 与 CLI 共用，消口径漂移）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Collection, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.config import (
    BacktestConfig,
    CostConfig,
    RiskConfig,
    SlippageConfig,
)
from ..core.engine import BacktestResult, run_backtest

__all__ = [
    "GRID_METRICS",
    "COMPARE_METRICS",
    "run_variants",
    "stress_config",
    "make_backtest_config",
    "rank_variants",
]

#: 网格/WF 消费的关键指标列（顺序即列顺序；新加指标只改这里）
GRID_METRICS: Tuple[str, ...] = (
    "累计收益率", "年化收益率", "最大回撤", "夏普比率", "卡玛比率",
    "交易次数", "胜率", "期末权益", "总手续费", "滑点成本",
)
#: 多策略对比/敏感性摘要用的精简列
COMPARE_METRICS: Tuple[str, ...] = ("累计收益率", "年化收益率", "最大回撤", "夏普比率", "卡玛比率", "交易次数")

#: 越大越好 / 越小越好（排名方向，唯一来源）
LOWER_IS_BETTER: Collection[str] = frozenset(
    {"最大回撤", "最长回撤天数", "总手续费", "滑点成本", "年化换手率(双边)", "平均持仓天数"}
)


def make_backtest_config(
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    cash: float = 1_000_000.0,
    execution: str = "next_open",
    commission: float = 2.5e-4,
    min_comm: float = 5.0,
    stamp: float = 5e-4,
    slip_model: str = "spread",
    slip_value: float = 5e-4,
    participation: float = 0.05,
    max_dd_halt: float = 0.0,
    benchmark: Optional[str] = None,
    block_limit: bool = True,
    liquidate_on_end: bool = True,
    warmup_bars: int = 0,
) -> BacktestConfig:
    """UI 侧栏 / CLI 参数 → BacktestConfig 的单一构造点。

    以前 UI 建 cfg 带成本/滑点/参与率，CLI 只建 start/end/cash/benchmark——
    同一研究在两个入口的摩擦假设可以悄悄不同。两侧都必须走这里。
    """
    return BacktestConfig(
        start_date=start or None,
        end_date=end or None,
        initial_cash=float(cash),
        execution=execution,
        cost=CostConfig(buy_rate=commission, sell_rate=commission, min_commission=min_comm,
                        stamp_tax_rate_sell=stamp),
        slippage=SlippageConfig(model=slip_model, value=slip_value),
        risk=RiskConfig(max_participation_rate=participation, max_drawdown_halt=max_dd_halt),
        benchmark=benchmark or None,
        block_limit_move=block_limit,
        liquidate_on_end=liquidate_on_end,
        warmup_bars=int(warmup_bars),
    )


def stress_config(config: BacktestConfig, multiplier: float = 2.0) -> BacktestConfig:
    """防自欺成本压测：佣金/最低佣/印花税/过户费/滑点整体放大（默认 ×2）。"""
    cost = replace(
        config.cost,
        buy_rate=config.cost.buy_rate * multiplier,
        sell_rate=config.cost.sell_rate * multiplier,
        min_commission=config.cost.min_commission * multiplier,
        stamp_tax_rate_sell=config.cost.stamp_tax_rate_sell * multiplier,
        transfer_fee_rate=config.cost.transfer_fee_rate * multiplier,
    )
    slip = replace(config.slippage, value=config.slippage.value * multiplier)
    return replace(config, cost=cost, slippage=slip)


def run_variants(
    variants: Sequence[Mapping],
    build: Callable[[Mapping], object],
    panel,
    config: BacktestConfig,
    metrics: Sequence[str] = GRID_METRICS,
    *,
    config_fn: Optional[Callable[[Mapping, BacktestConfig], BacktestConfig]] = None,
    lite: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[pd.DataFrame, List[Optional[BacktestResult]]]:
    """对每个变体 dict 跑一次回测，返回 (行表, 结果对象列表)。

    variants : 变体参数（dict，键会原样进 DataFrame 列，如 {"fast":5} 或 {"策略":"动量"}）
    build    : variant -> Strategy
    config_fn: variant -> BacktestConfig（成本压测等需要按变体改配置的场景；缺省复用 config）
    lite     : 引擎轻量模式（只要指标时必开；对比曲线需要 equity 时也无碍——equity 仍保留）

    错误不抛穿：失败变体记 `_error` 行，结果对象为 None，整批不中断。
    """
    rows: List[dict] = []
    results: List[Optional[BacktestResult]] = []
    n = len(variants)
    for i, v in enumerate(variants, 1):
        try:
            cfg = config_fn(v, config) if config_fn else config
            result = run_backtest(build(v), panel, cfg, lite=lite)
            row = dict(v)
            row.update({k: result.metrics.get(k) for k in metrics})
            row["_error"] = ""
            rows.append(row)
            results.append(result)
        except Exception as e:  # noqa: BLE001 —— 参数不合法等保留记录，不中断批跑
            row = dict(v)
            row.update({k: None for k in metrics})
            row["_error"] = f"{type(e).__name__}: {e}"
            rows.append(row)
            results.append(None)
        if progress:
            progress(i, n)
    return pd.DataFrame(rows), results


def rank_variants(
    df: pd.DataFrame,
    grid_keys: Sequence[str],
    rank_by: str = "夏普比率",
    min_trades: int = 0,
) -> pd.DataFrame:
    """按 rank_by 排序并加 `_score`/`排名`。第 1 名只是候选，邻域成片才是信号。"""
    if rank_by in df.columns:
        score = pd.to_numeric(df[rank_by], errors="coerce").astype(float)
    else:
        score = pd.Series(np.nan, index=df.index)
    ok = (df["_error"] == "") if "_error" in df.columns else pd.Series(True, index=df.index)
    good = ok & score.notna()
    if rank_by in LOWER_IS_BETTER:
        df["_score"] = np.where(good, -score.fillna(0.0), -np.inf)
    else:
        df["_score"] = np.where(good, score.fillna(-np.inf), -np.inf)
    if min_trades and "交易次数" in df.columns:
        few = df["交易次数"].fillna(0) < min_trades
        df.loc[few, "_score"] = -np.inf
    df = df.sort_values("_score", ascending=False).reset_index(drop=True)
    df.insert(0, "排名", range(1, len(df) + 1))
    return df
