"""复权与数据清洗。

* `apply_adjustment`：按复权因子生成 hfq / qfq / none 三种价格序列
* `clean_bars`：去重、排序、剔除非正价格、补齐 pre_close、标记停牌
* `A_SHARE_CALENDAR_NOTE`：交易日历说明（MVP 用「有行情的日期并集」代替官方日历）

为什么默认推荐 **后复权（hfq）** 做引擎内部计算：
  前复权会随新的分红送转而整体改写历史价格，同一段历史在不同时间点回测会得到
  不同结果（不可复现）；后复权的历史是不变量，结果可复现。展示层再换算回前复权。
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import pandas as pd

__all__ = ["apply_adjustment", "clean_bars", "suspended_mask", "A_SHARE_CALENDAR_NOTE"]

A_SHARE_CALENDAR_NOTE = (
    "MVP 以「全部标的出现行情的日期并集」作为交易日历；生产环境应替换为交易所日历"
    "（shcal / 中国工具 `exchange_calendars` 的 XSHG/XSHE），并注意临时停牌、"
    "节假日调休、年末最后交易日等边界。"
)

_PRICE_COLS = ["open", "high", "low", "close", "pre_close"]


def clean_bars(
    df: pd.DataFrame,
    symbol: Optional[str] = None,
    min_volume_is_suspend: bool = True,
) -> pd.DataFrame:
    """清洗单标的日线表：列名需为 canonical（open/high/low/close/volume/amount/pre_close）。"""
    out = df.copy()
    if "pre_close" not in out.columns:
        out["pre_close"] = np.nan
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    for col in ["open", "high", "low", "close", "volume", "amount", "pre_close"]:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # 价格非正 → 视为无效（保留行但标记停牌）
    bad = (out["close"] <= 0) | out["close"].isna()
    out.loc[bad, ["open", "high", "low", "close", "volume"]] = np.nan
    # high/low 修正：包住 open/close
    valid = out["close"].notna()
    out.loc[valid, "high"] = out.loc[valid, ["high", "open", "close", "low"]].max(axis=1)
    out.loc[valid, "low"] = out.loc[valid, ["low", "open", "close"]].min(axis=1)
    if min_volume_is_suspend:
        out.loc[out["volume"].fillna(0) <= 0, ["open", "high", "low", "close", "volume"]] = np.nan
    out["amount"] = out["amount"].fillna(0.0)
    out["volume"] = out["volume"].astype("float64")
    # 昨收：优先用数据自带，缺失则用上一交易日收盘（同股）。
    # 兜底取 close.ffill().shift(1) 而非 close.shift(1)：volume<=0 的停牌行 OHLC
    # 已被置 NaN，直接 shift 会在复牌日取到停牌行的 NaN → 复牌日昨收缺失 →
    # 引擎回退用当日收盘当昨收，涨跌停价永远不触发，复牌一字板封板判定失效
    # （真实买不进的封板单被成交）。ffill 跳过停牌行取停牌前最后有效收盘=真实昨收。
    out["pre_close"] = out["pre_close"].where(out["pre_close"] > 0, out["close"].ffill().shift(1))
    if symbol:
        out["symbol"] = symbol
    return out


def suspended_mask(df: pd.DataFrame) -> pd.Series:
    return df["close"].isna() | df["volume"].fillna(0).le(0)


def apply_adjustment(
    df: pd.DataFrame,
    adj_factor: pd.Series,
    mode: str = "qfq",
    base_date: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """用复权因子调整价格列。

    约定 `adj_factor` 为**累计后复权因子**（越大代表历史除权越多），
    满足：hfq_price = raw_price × adj_factor / adj_factor[base]，base 为锚定日处的因子：
      * hfq（后复权）：base 默认取**最早**有效因子——历史是不变量，锚点日价格 = raw；
      * qfq（前复权）：base 默认取**最新**有效因子——最新价不动，历史价整体缩小；
      * `base_date` 显式指定锚点（两模式通用）：锚点日价格 = raw，其余按因子比缩放。
    若你手上是「除权除息比例表」，先用 `factor_from_ratios` 转成累计因子。
    """
    mode = (mode or "qfq").lower()
    out = df.copy()
    if mode in ("none", "raw", ""):
        return out
    f = pd.to_numeric(adj_factor, errors="coerce").reindex(out.index).ffill().bfill()
    if f.isna().all():
        return out
    if base_date is not None:
        # 显式锚点：两模式同一语义——锚定 base_date 处因子，锚点日价格 = raw
        base = f.loc[:base_date].dropna().iloc[-1]
    elif mode == "hfq":
        base = f.iloc[0]   # 锚定最早因子：后复权，历史价随因子放大（不变量，可复现）
    elif mode == "qfq":
        base = f.iloc[-1]  # 锚定最新因子：前复权，历史价整体缩小
    else:
        raise ValueError(f"未知复权方式: {mode}")
    ratio = (f / base).to_numpy(dtype=float)
    for col in _PRICE_COLS:
        if col in out.columns:
            out[col] = out[col] * ratio
    return out


def factor_from_ratios(ratios: pd.Series) -> pd.Series:
    """把「除权除息比例（pre_close/close_前一交易日）」序列转成累计后复权因子。"""
    return (1.0 / pd.to_numeric(ratios, errors="coerce").fillna(1.0)).cumprod()


def to_panel_input(frames: Iterable, symbols: Optional[Iterable[str]] = None) -> dict:
    """把 [(symbol, df), ...] 或 dict 统一成 dict。"""
    if isinstance(frames, dict):
        return dict(frames)
    return {str(s): df for s, df in zip(symbols or [], frames)}
