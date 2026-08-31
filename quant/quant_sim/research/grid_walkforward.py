"""研究闭环：参数网格搜索与 Walk-Forward 滚动样本外验证。

设计动机（对应 docs/design.md §7 防过拟合）：
  * grid_search：量化"参数平面长什么样"——好结果若只出现在孤立尖峰，基本是过拟合；
    稳健策略应在一片邻域内指标连续、不剧烈翻转。
  * walk_forward：滚动「训练窗优化 → 紧邻测试窗验证」，是唯一能给出
    「样本外净值曲线」的朴素手段；输出 IS→OOS 衰减率作为过拟合诊断。

注意：grid_search 的排名只是研究素材，不构成选型结论；请配合参数邻域
稳定性与 walk_forward 的 OOS 表现一起判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product
from typing import Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.config import BacktestConfig
from ..core.engine import BacktestResult, run_backtest
from ..core.strategy_base import Strategy
from .runner import (  # 排名方向与指标列的唯一来源（从本模块迁入，保留旧导入路径）
    LOWER_IS_BETTER as _LOWER_IS_BETTER,
    GRID_METRICS,
    rank_variants,
    run_variants,
)

__all__ = ["grid_search", "make_folds", "walk_forward", "WalkForwardResult", "param_grid"]


def param_grid(**axes: Sequence) -> List[Dict]:
    """笛卡尔积参数组合列表。grid_search 内部使用，也可手工传入。"""
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in product(*(axes[k] for k in keys))]


# 越大越好 / 越小越好：已收敛到 research.runner.LOWER_IS_BETTER（本名保留为兼容别名）


def _rank_value(result: BacktestResult, metric: str) -> float:
    v = result.metrics.get(metric)
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return -np.inf if metric not in _LOWER_IS_BETTER else np.inf
    return float(v)


def grid_search(
    strategy_factory: Callable[..., Strategy],
    grid: Mapping[str, Sequence],
    data,
    config: BacktestConfig,
    rank_by: str = "夏普比率",
    max_trades_filter: int = 0,
    progress: Optional[Callable[[int, int], None]] = None,
) -> pd.DataFrame:
    """参数网格回测，返回按 rank_by 排序的结果表。

    参数
    ----
    strategy_factory : (**combo) -> Strategy。每个组合新建独立实例。
    grid             : {"fast": [5, 10, 20], "slow": [40, 60, 120]}
    data             : BarPanel / DataFrame / {symbol: df}
    config           : 基准配置（start/end/cost/risk 等）；每个组合复用同一份
    rank_by          : metrics 里的中文指标名
    max_trades_filter: 交易次数低于该值的组合沉底（样本太少不可信）
    """
    combos = param_grid(**grid)
    df, _results = run_variants(
        combos,
        build=lambda v: strategy_factory(**{k: v[k] for k in grid}),
        panel=data,
        config=config,
        metrics=GRID_METRICS,
        lite=True,
        progress=progress,
    )
    return rank_variants(df, list(grid), rank_by=rank_by, min_trades=max_trades_filter)


# =====================================================================================
# Walk-Forward
# =====================================================================================
@dataclass
class WalkForwardResult:
    folds: pd.DataFrame                    # 每折：训练/测试窗（结构化日期列+展示列）/最优参数/IS/OOS 指标/衰减率
    oos_equity: pd.Series                  # 拼接后的纯样本外净值（起点=初始资金）
    oos_metrics: Dict[str, float]          # OOS 整体绩效（与全样本指标同口径）
    overfit_ratio: Optional[float]         # mean(IS)/mean(OOS)，rank_by 越大越好时才有意义
    rank_by: str = "夏普比率"
    best_params_per_fold: List[Dict] = field(default_factory=list)
    warmup_days: int = 0                   # 每折数据窗前推的预热 bar 数
    decay_warn: float = 2.0                # IS/OOS 衰减告警阈值（结构化判定参数）
    verdict: Dict[str, object] = field(default_factory=dict)        # {level, reason, threshold}
    param_stability: List[Dict] = field(default_factory=list)       # 相邻折最优参数迁移统计

    def summary(self) -> str:
        lines = [
            "=" * 62,
            f"Walk-Forward 结果（排名指标：{self.rank_by}；每折预热 {self.warmup_days} bar）",
            "=" * 62,
            self.folds.to_string(index=False),
            "",
            "样本外（OOS 拼接）整体指标：",
        ]
        for k in ["累计收益率", "年化收益率", "最大回撤", "夏普比率", "交易次数(各折合计)"]:
            v = self.oos_metrics.get(k)
            if v is None:
                continue
            if k.startswith("交易次数"):
                lines.append(f"  {k:<14} {int(v)}")
            else:
                lines.append(f"  {k:<14} {v:.4f}")
        if self.param_stability:
            lines.append("\n相邻折最优参数迁移（跳变频繁 = 参数本身在拟合噪声）：")
            for ps in self.param_stability:
                lines.append(f"  折 {ps['折间']}：改动 {ps['改动数']}/{ps['参数数']} → {ps['明细']}")
        v = self.verdict or _verdict(self.overfit_ratio, self.rank_by, self.decay_warn)
        icon = {"ok": "✓", "warn": "⚠️", "unknown": "❓"}.get(v["level"], "")
        lines.append(f"\n过拟合诊断 overfit_ratio = {v['value']}：{icon} {v['reason']}（阈值 {v['threshold']}）")
        lines.append("=" * 62)
        return "\n".join(lines)


def make_folds(dates: pd.DatetimeIndex, train_days: int = 244, test_days: int = 63, step_days: Optional[int] = None) -> List[Dict[str, pd.Timestamp]]:
    """滚动窗口切分（交易日数）。默认约 1 年训练 + 1 季度测试、测试窗不重叠。"""
    step = step_days or test_days
    if step < test_days:
        raise ValueError(
            f"step_days({step}) < test_days({test_days})：测试窗重叠会让同一天样本外收益被拼接多次，"
            "污染 OOS 曲线。如需更密滚动，请缩短 test_days。"
        )
    folds: List[Dict[str, pd.Timestamp]] = []
    i = 0
    while i + train_days + test_days <= len(dates):
        folds.append(
            {
                "train_start": dates[i],
                "train_end": dates[i + train_days - 1],
                "test_start": dates[i + train_days],
                "test_end": dates[min(i + train_days + test_days, len(dates)) - 1],
            }
        )
        i += step
    return folds


def _verdict(ratio: Optional[float], rank_by: str, decay_warn: float) -> Dict[str, object]:
    """结构化的过拟合诊断（裁决不嵌在展示文案里，UI/CLI/测试各自渲染）。"""
    if ratio is None or not np.isfinite(ratio):
        return {"level": "unknown", "value": None, "threshold": decay_warn,
                "reason": "训练窗/测试窗平均指标非正或不可比，无法计算衰减率（该市场阶段策略本就无效时换参数无意义）"}
    if float(ratio) > decay_warn:
        return {"level": "warn", "value": round(float(ratio), 4), "threshold": decay_warn,
                "reason": "IS→OOS 明显衰减，警惕过拟合"}
    return {"level": "ok", "value": round(float(ratio), 4), "threshold": decay_warn,
            "reason": "IS/OOS 衰减在可接受范围"}


def _auto_warmup_days(grid: Mapping[str, Sequence]) -> int:
    """按网格里的最大数值参数推预热长度（覆盖最长回看窗口 + 缓冲，下限 40）。"""
    vals = [float(v) for vs in grid.values() for v in vs
            if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool)]
    return int(max(40, (max(vals) if vals else 20) + 10))


def walk_forward(
    strategy_factory: Callable[..., Strategy],
    grid: Mapping[str, Sequence],
    panel,
    base_config: BacktestConfig,
    train_days: int = 244,
    test_days: int = 63,
    step_days: Optional[int] = None,
    rank_by: str = "夏普比率",
    progress: Optional[Callable[[int, int], None]] = None,
    warmup_days: Optional[int] = None,
    decay_warn: float = 2.0,
) -> WalkForwardResult:
    """滚动样本外验证：每折用 train 窗网格选参 → 立刻在紧邻 test 窗验证。

    每折数据窗从信号窗前推 warmup_days 根 bar（缺省按网格最大数值参数自动估）：
    策略在预热段真实建立仓位，长回看参数不会在窗口头部被「饿死」，
    OOS 拼接首段也不再是人为拍平的空仓收益；指标只统计窗内。
    OOS 净值 = 各折测试窗日收益拼接复利（首日相对预热末权益，非初始资金），全程不接触未来。
    """
    data = panel  # BarPanel（或可被 run_backtest 接受的数据）
    dates = pd.DatetimeIndex(getattr(data, "dates", None))
    if dates is None or len(dates) == 0:
        raise ValueError("walk_forward 需要带 dates 轴的 BarPanel")
    if base_config.start_date:
        dates = dates[dates >= pd.Timestamp(base_config.start_date).normalize()]
    if base_config.end_date:
        dates = dates[dates <= pd.Timestamp(base_config.end_date).normalize()]
    folds_def = make_folds(dates, train_days, test_days, step_days)
    if not folds_def:
        raise ValueError("数据长度不足以切出一个 train+test 折，请缩短窗口或加长数据")
    warm = int(warmup_days) if warmup_days is not None else _auto_warmup_days(grid)

    fold_rows: List[dict] = []
    best_list: List[Dict] = []
    oos_returns: List[pd.Series] = []
    is_scores, oos_scores = [], []
    res_trade_counts: List[float] = []

    for fi, win in enumerate(folds_def, 1):
        cfg_is = replace(base_config, start_date=str(win["train_start"].date()), end_date=str(win["train_end"].date()), warmup_bars=warm)
        grid_df = grid_search(strategy_factory, grid, data, cfg_is, rank_by=rank_by)
        top = grid_df.iloc[0]
        best_params = _coerce({k: top[k] for k in grid})
        is_score = float(top["_score"]) if np.isfinite(top["_score"]) else np.nan

        cfg_os = replace(base_config, start_date=str(win["test_start"].date()), end_date=str(win["test_end"].date()), warmup_bars=warm)
        oos = run_backtest(strategy_factory(**best_params), data, cfg_os)
        oos_score = _rank_value(oos, rank_by)
        # 拼接用完整净值（含预热段）：测试窗首日收益相对预热末日权益，
        # 而非相对初始资金的假收益（旧口径把折首段强制拍平，系统性失真）。
        src_eq = oos.equity_full if oos.equity_full is not None else oos.equity
        daily_ret = src_eq.pct_change()
        daily_ret = daily_ret[(daily_ret.index >= win["test_start"]) & (daily_ret.index <= win["test_end"])]
        if len(daily_ret):
            daily_ret = daily_ret.copy()
            daily_ret.iloc[0] = float(daily_ret.iloc[0]) if np.isfinite(daily_ret.iloc[0]) else 0.0
        decay = None
        if np.isfinite(is_score) and np.isfinite(oos_score) and is_score > 0 and oos_score > 0:
            decay = round(float(is_score) / float(oos_score), 4)
        oos_returns.append(daily_ret.fillna(0.0))

        fold_rows.append(
            {
                "折": fi,
                "训练窗起": win["train_start"].date().isoformat(),
                "训练窗止": win["train_end"].date().isoformat(),
                "测试窗起": win["test_start"].date().isoformat(),
                "测试窗止": win["test_end"].date().isoformat(),
                "训练窗": f"{win['train_start'].date()}~{win['train_end'].date()}",
                "测试窗": f"{win['test_start'].date()}~{win['test_end'].date()}",
                "最优参数": str(best_params),
                f"IS_{rank_by}": is_score,
                f"OOS_{rank_by}": oos_score,
                "衰减率": decay,
                "OOS累计收益": oos.metrics.get("累计收益率"),
                "OOS最大回撤": oos.metrics.get("最大回撤"),
                "OOS交易次数": oos.metrics.get("交易次数"),
            }
        )
        best_list.append(best_params)
        res_trade_counts.append(oos.metrics.get("交易次数", 0) or 0)
        if np.isfinite(is_score):
            is_scores.append(is_score)
        if np.isfinite(oos_score):
            oos_scores.append(oos_score)
        if progress:
            progress(fi, len(folds_def))

    all_ret = pd.concat(oos_returns).sort_index()
    all_ret = all_ret[~all_ret.index.duplicated(keep="first")]
    oos_equity = (1.0 + all_ret).cumprod() * base_config.initial_cash

    from ..metrics.performance import compute_metrics

    frame = pd.DataFrame({"equity": oos_equity, "cash": oos_equity, "holdings_value": 0.0})
    oos_metrics = compute_metrics(frame, [], account=None, config=base_config)
    oos_metrics["交易次数(各折合计)"] = int(sum(int(r) for r in res_trade_counts))
    ratio = None
    if rank_by not in _LOWER_IS_BETTER and is_scores and oos_scores:
        mean_is, mean_oos = float(np.mean(is_scores)), float(np.mean(oos_scores))
        if mean_is > 0 and mean_oos > 0:
            ratio = mean_is / mean_oos
    # 相邻折最优参数迁移：频繁跳变 = 参数在拟合噪声（数据已在手，不该只丢给用户自己看）
    stability: List[Dict] = []
    for a, b in zip(best_list, best_list[1:]):
        changed = {k: [a.get(k), b.get(k)] for k in set(a) | set(b) if a.get(k) != b.get(k)}
        stability.append({
            "折间": f"{len(stability) + 1}→{len(stability) + 2}",
            "改动数": len(changed),
            "参数数": len(set(a) | set(b)),
            "明细": str(changed) if changed else "完全一致",
        })
    return WalkForwardResult(
        folds=pd.DataFrame(fold_rows),
        oos_equity=oos_equity,
        oos_metrics=oos_metrics,
        overfit_ratio=ratio,
        rank_by=rank_by,
        best_params_per_fold=best_list,
        warmup_days=warm,
        decay_warn=decay_warn,
        verdict=_verdict(ratio, rank_by, decay_warn),
        param_stability=stability,
    )


def _coerce(params: Dict):
    """网格结果经 DataFrame 后 numpy 类型回转 python，避免工厂断言意外。"""
    out = {}
    for k, v in params.items():
        if isinstance(v, np.integer):
            out[k] = int(v)
        elif isinstance(v, np.floating):
            out[k] = float(v)
        else:
            out[k] = v
    return out
