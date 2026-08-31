"""绩效指标。

口径统一（写死在这里，避免各处不一致）：
  * 年化 = 复利年化，一年交易日数取 config.trading_days_per_year（默认 244）
  * 波动率 = 日收益率标准差（ddof=1）× sqrt(年化天数)
  * 夏普 = (年化收益 - 无风险利率) / 年化波动
  * 索提诺 = (年化收益 - 无风险利率) / 下行波动（仅对负收益取标准差）
  * 最大回撤 = 1 - min(equity / cummax(equity))
  * 卡玛 = 年化收益 / 最大回撤
  * 胜率/盈亏比基于 FIFO 配对的 Trade（含费用）
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "daily_returns",
    "drawdown_series",
    "max_drawdown",
    "annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "trade_stats",
    "compute_metrics",
    "make_metrics_dataframe",
]


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.astype(float).pct_change().dropna()


def drawdown_series(equity: pd.Series) -> pd.Series:
    eq = equity.astype(float)
    peak = eq.cummax()
    return (eq / peak - 1.0).rename("drawdown")


def max_drawdown(equity: pd.Series) -> float:
    dd = drawdown_series(equity)
    return float(-dd.min()) if len(dd) else 0.0


def _periods_per_year(config) -> int:
    return int(getattr(config, "trading_days_per_year", 244) or 244)


def annualized_return(equity: pd.Series, config=None) -> float:
    if len(equity) < 2:
        return 0.0
    total = float(equity.iloc[-1] / equity.iloc[0])
    n = len(equity) - 1
    ppy = _periods_per_year(config)
    if total <= 0 or n <= 0:
        return -1.0
    return float(total ** (ppy / n) - 1.0)


def annualized_volatility(returns: pd.Series, config=None) -> float:
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=1) * math.sqrt(_periods_per_year(config)))


def sharpe_ratio(returns: pd.Series, config=None) -> float:
    rf_daily = float(getattr(config, "risk_free_rate", 0.0) or 0.0) / _periods_per_year(config)
    excess = returns - rf_daily
    vol = excess.std(ddof=1)
    if vol is None or not np.isfinite(vol) or vol < 1e-12:
        return 0.0
    return float(excess.mean() / vol * math.sqrt(_periods_per_year(config)))


def sortino_ratio(returns: pd.Series, config=None) -> float:
    rf_daily = float(getattr(config, "risk_free_rate", 0.0) or 0.0) / _periods_per_year(config)
    excess = returns - rf_daily
    downside = excess[excess < 0]
    if len(downside) < 2:
        return float("inf") if excess.mean() > 0 else 0.0
    dvol = math.sqrt(float((downside ** 2).mean())) * math.sqrt(_periods_per_year(config))
    ann = float(excess.mean() * _periods_per_year(config))
    return ann / dvol if dvol else 0.0


def calmar_ratio(equity: pd.Series, config=None) -> float:
    dd = max_drawdown(equity)
    if dd <= 0:
        return float("inf")
    return annualized_return(equity, config) / dd


def monthly_returns(equity: pd.Series) -> pd.Series:
    m = equity.astype(float).resample("ME").last()
    return m.pct_change().dropna()


def trade_stats(trades: Sequence) -> Dict[str, float]:
    if not len(trades):
        return {
            "交易次数": 0,
            "胜率": np.nan,
            "盈亏比": np.nan,
            "平均单笔收益率": np.nan,
            "平均持仓天数": np.nan,
            "单笔最大盈利": np.nan,
            "单笔最大亏损": np.nan,
            "总盈利": np.nan,
            "总亏损": np.nan,
        }
    pnl = np.array([t.net_pnl for t in trades], dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    pl_ratio = (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if avg_win > 0 else np.nan)
    rets = np.array([t.return_pct for t in trades], dtype=float)
    return {
        "交易次数": int(len(pnl)),
        "胜率": float(len(wins) / len(pnl)),
        "盈亏比": float(pl_ratio) if np.isfinite(pl_ratio) else np.inf,
        "平均单笔收益率": float(rets.mean()),
        "平均持仓天数": float(np.mean([t.holding_days for t in trades])),
        "单笔最大盈利": float(pnl.max()),
        "单笔最大亏损": float(pnl.min()),
        "总盈利": gross_profit,
        "总亏损": -gross_loss,
    }


def compute_metrics(
    equity_frame: pd.DataFrame,
    trades: List,
    account=None,
    config=None,
    benchmark: Optional[pd.Series] = None,
) -> Dict[str, float]:
    equity = equity_frame["equity"].astype(float)
    returns = daily_returns(equity)
    ann_ret = annualized_return(equity, config)
    vol = annualized_volatility(returns, config)
    dd = max_drawdown(equity)
    metrics: Dict[str, float] = {
        "回测区间": f"{equity.index[0].date()} ~ {equity.index[-1].date()}",
        "交易日数": int(len(equity)),
        "初始资金": float(equity.iloc[0] if len(equity) else 0.0),
        "期末权益": float(equity.iloc[-1]),
        "累计收益率": float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) and equity.iloc[0] else 0.0,
        "年化收益率": ann_ret,
        "年化波动率": vol,
        "最大回撤": dd,
        "最长回撤天数": _max_dd_duration(equity),
        "夏普比率": sharpe_ratio(returns, config),
        "索提诺比率": sortino_ratio(returns, config),
        "卡玛比率": (ann_ret / dd) if dd > 0 else float("inf"),
        "日均收益": float(returns.mean()) if len(returns) else 0.0,
        "日胜率": float((returns > 0).mean()) if len(returns) else 0.0,
        "月度正收益占比": float((monthly_returns(equity) > 0).mean()) if len(monthly_returns(equity)) else np.nan,
    }
    metrics.update(trade_stats(trades))
    if account is not None:
        metrics["总手续费"] = float(account.total_costs())
        metrics["其中佣金"] = float(account.total_commission)
        metrics["其中印花税"] = float(account.total_stamp_tax)
        metrics["其中过户费"] = float(account.total_transfer_fee)
        metrics["滑点成本"] = float(account.total_slippage)
        turnover = account.turnover()
        avg_equity = float(equity.mean()) if len(equity) else 0.0
        years = (len(equity) / _periods_per_year(config)) if len(equity) else 1.0
        metrics["年化换手率(双边)"] = float(turnover / avg_equity / years) if avg_equity and years else np.nan
        metrics["费用占初始资金比"] = float(account.total_costs() / (equity.iloc[0] or 1.0))
    if benchmark is not None and len(benchmark.dropna()) > 2:
        bench = benchmark.dropna()
        bench = bench.reindex(equity.index).ffill()
        bench_equity = bench / bench.dropna().iloc[0] * equity.iloc[0]
        bench_ret = daily_returns(bench_equity)
        strat_ret = returns.reindex(bench_ret.index).dropna()
        b2 = bench_ret.reindex(strat_ret.index)
        if len(strat_ret) > 2 and float(b2.std()) > 1e-12 and float(strat_ret.std()) > 1e-12:
            cov = float(np.cov(strat_ret, b2)[0, 1])
            beta = cov / float(np.var(b2, ddof=1))
            corr = float(np.corrcoef(strat_ret, b2)[0, 1])
            te = float((strat_ret - b2).std(ddof=1) * math.sqrt(_periods_per_year(config)))
            active = float((strat_ret - b2).mean() * _periods_per_year(config))
            metrics.update(
                {
                    "基准年化收益": annualized_return(bench_equity, config),
                    "Beta": beta,
                    "跟踪误差": te,
                    "信息比率": (active / te) if te else np.nan,
                    "年化超额收益": active,
                    "相关性": corr,
                    "阿尔法(CAPM)": float(strat_ret.mean() * _periods_per_year(config) - float(getattr(config, "risk_free_rate", 0.0) or 0.0)
                        - beta * (annualized_return(bench_equity, config) - float(getattr(config, "risk_free_rate", 0.0) or 0.0))),
                }
            )
    return metrics


def _max_dd_duration(equity: pd.Series) -> int:
    """最长水下天数（从创新高到重新解套）。"""
    eq = equity.astype(float)
    peak = eq.cummax()
    underwater = eq < peak
    best = cur = 0
    for flag in underwater.to_numpy():
        cur = cur + 1 if flag else 0
        best = max(best, cur)
    return int(best)


def make_metrics_dataframe(metrics: Dict[str, float]) -> pd.DataFrame:
    order = [
        "回测区间", "交易日数", "初始资金", "期末权益", "累计收益率", "年化收益率", "年化波动率",
        "最大回撤", "最长回撤天数", "夏普比率", "索提诺比率", "卡玛比率", "日均收益", "日胜率",
        "月度正收益占比", "交易次数", "胜率", "盈亏比", "平均单笔收益率", "平均持仓天数",
        "单笔最大盈利", "单笔最大亏损", "总盈利", "总亏损", "总手续费", "其中佣金", "其中印花税",
        "其中过户费", "滑点成本", "年化换手率(双边)", "费用占初始资金比",
        "基准年化收益", "年化超额收益", "Beta", "跟踪误差", "信息比率", "相关性", "阿尔法(CAPM)",
    ]
    rows = []
    for key in order + [k for k in metrics if k not in order]:
        if key in metrics:
            rows.append({"指标": key, "数值": metrics[key]})
    df = pd.DataFrame(rows).drop_duplicates(subset="指标", keep="first")
    df["展示值"] = [_display(r["指标"], r["数值"]) for _, r in df.iterrows()]
    return df[["指标", "数值", "展示值"]]


PERCENT_KEYS = (
    "收益率", "回撤", "率", "占比", "胜率", "换手", "超额", "跟踪误差", "费用占初始资金比",
)


def _display(key: str, value) -> str:
    """已收敛到 report.formatter.display_value（单一实现）；本名保留为兼容入口。"""
    from ..report.formatter import display_value

    return display_value(key, value)
