"""归因分析：回答「钱是怎么赚/亏的」。

- 月度收益热力图矩阵（年 × 月）
- 持仓周期分布与分桶胜率
- 成本侵蚀：佣金/印花税/过户费/滑点各吃掉多少，占毛利比例
- 最佳/最差交易榜、按标的盈亏汇总

输入都是 BacktestResult，输出 DataFrame/dict，界面与 CLI 共用。
"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    """月胜率/月度收益矩阵：行=年，列=1-12 月，值=当月收益率；末列为全年。"""
    r = equity.astype(float)
    m_end = r.resample("ME").last()
    m = m_end / m_end.shift(1) - 1
    if len(m):
        m.iloc[0] = m_end.iloc[0] / float(r.iloc[0]) - 1
    m = m.dropna()
    tbl = m.to_frame("ret")
    tbl["y"] = tbl.index.year
    tbl["m"] = tbl.index.month
    piv = tbl.pivot_table(index="y", columns="m", values="ret", aggfunc="first")
    piv.columns = [f"{c}月" for c in piv.columns]
    annual = m_end.groupby(m_end.index.year).apply(
        lambda end_vals: end_vals.iloc[-1] / r[r.index.year == end_vals.name].iloc[0] - 1
    )
    piv["全年"] = annual.round(4)
    return piv


def holding_stats(trades: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """持仓天数分布 + 分桶胜率/期望。trades 需含 entry_date/exit_date/net_pnl。"""
    if trades is None or not len(trades):
        return {"summary": pd.DataFrame(), "buckets": pd.DataFrame()}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"])
    t["exit_date"] = pd.to_datetime(t["exit_date"])
    t["holding_days"] = (t["exit_date"] - t["entry_date"]).dt.days
    bins = [0, 5, 10, 20, 40, 90, 10_000]
    labels = ["≤5天", "5-10天", "10-20天", "20-40天", "40-90天", ">90天"]
    t["bucket"] = pd.cut(t["holding_days"], bins=bins, labels=labels, include_lowest=True)
    g = t.groupby("bucket", observed=True).agg(
        交易数=("net_pnl", "size"),
        胜率=("net_pnl", lambda s: float((s > 0).mean())),
        平均收益=("return_pct", "mean"),
        合计盈亏=("net_pnl", "sum"),
    )
    summary = pd.DataFrame({
        "指标": ["平均持仓天数", "中位数持仓天数", "最长持仓", "最短持仓", "交易总数"],
        "值": [t["holding_days"].mean(), t["holding_days"].median(), t["holding_days"].max(), t["holding_days"].min(), len(t)],
    })
    return {"summary": summary, "buckets": g}


def cost_drag(result) -> pd.DataFrame:
    """成本侵蚀报告：各类费用合计、占权益/占毛利比例。"""
    fills: pd.DataFrame = result.fills
    eq0 = float(result.equity.iloc[0])
    gross = float(fills["gross_pnl"].sum()) if "gross_pnl" in fills.columns else np.nan
    if np.isnan(gross):
        # 用 trades 估算毛利：净盈亏+成本 就是毛口径
        gross = float(result.trades["net_pnl"].sum() + result.trades["cost"].sum()) if len(result.trades) else 0.0
    total_slip = float(fills["slippage_cost"].sum()) if "slippage_cost" in fills.columns else 0.0
    rows = []
    for label, col in [("佣金", "commission"), ("印花税", "stamp_tax"), ("过户费", "transfer_fee"), ("滑点", "slippage_cost")]:
        amt = float(fills[col].sum()) if col in fills.columns and len(fills) else 0.0
        rows.append({"项目": label, "金额": amt, "占初始权益": amt / eq0 if eq0 else 0.0})
    total = sum(r["金额"] for r in rows)
    rows.append({"项目": "合计", "金额": total, "占初始权益": total / eq0 if eq0 else 0.0})
    df = pd.DataFrame(rows)
    erode = total / gross if gross and gross > 0 else np.nan
    df["占毛利"] = [erode if r["项目"] == "合计" else np.nan for r in rows]
    return df


def trade_leaderboard(trades: pd.DataFrame, n: int = 5) -> Dict[str, pd.DataFrame]:
    """最佳/最差 n 笔 + 按标的盈亏汇总。"""
    empty = pd.DataFrame()
    if trades is None or not len(trades):
        return {"best": empty, "worst": empty, "by_symbol": empty}
    cols = [c for c in ["symbol", "entry_date", "exit_date", "quantity", "return_pct", "net_pnl", "cost"] if c in trades.columns]
    t = trades[cols].copy()
    best = t.nlargest(n, "net_pnl")
    worst = t.nsmallest(n, "net_pnl")
    by_symbol = trades.groupby("symbol").agg(
        交易次数=("net_pnl", "size"), 胜率=("net_pnl", lambda s: float((s > 0).mean())), 合计盈亏=("net_pnl", "sum")
    ).sort_values("合计盈亏", ascending=False)
    return {"best": best, "worst": worst, "by_symbol": by_symbol}
