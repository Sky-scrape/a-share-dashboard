"""研究闭环：网格搜索、Walk-Forward、统一变体 runner 与因子工作台。"""

from .factor import FACTORS, eval_expression, factor_matrix, run_factor_study
from .grid_walkforward import grid_search, make_folds, param_grid, walk_forward, WalkForwardResult
from .runner import (
    COMPARE_METRICS,
    GRID_METRICS,
    LOWER_IS_BETTER,
    make_backtest_config,
    rank_variants,
    run_variants,
    stress_config,
)

__all__ = [
    "grid_search", "make_folds", "param_grid", "walk_forward", "WalkForwardResult",
    "FACTORS", "eval_expression", "factor_matrix", "run_factor_study",
    "run_variants", "rank_variants", "make_backtest_config", "stress_config",
    "GRID_METRICS", "COMPARE_METRICS", "LOWER_IS_BETTER",
]
