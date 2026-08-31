"""合成 A 股日线样例数据（离线可用，规则真实）。

用途：
  * 无网络/无数据源时跑通引擎、界面、测试
  * 内置了 A 股特征的“压力场景”：涨跌停封板、连续停牌、除权除息（10 送 5 + 分红）

⚠️ 这是**随机生成**的模拟行情，只能验证引擎正确性与演示用法，
   **不能**用来评价策略的真实盈利能力。正式研究请用 akshare/tushare/券商数据。
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from ..core.types import BarPanel

__all__ = ["make_synthetic_panel", "make_demo_panel", "save_demo_data"]

#: 演示池：宽基/行业 ETF + 个股（代码为真实代码，价格为合成）
DEMO_SYMBOLS: Tuple[Tuple[str, str], ...] = (
    ("510300", "沪深300ETF"),
    ("510500", "中证500ETF"),
    ("159915", "创业板ETF"),
    ("512880", "证券ETF"),
    ("512690", "酒ETF"),
    ("588000", "科创50ETF"),
    ("600519", "贵州茅台*"),
    ("300750", "宁德时代*"),
)


def _trading_dates(start: str, end: str) -> pd.DatetimeIndex:
    dates = pd.bdate_range(start, end)  # 周一~周五；真实日历见 docs/数据契约
    return dates


def make_synthetic_panel(
    n_symbols: int = 8,
    start: str = "2019-01-02",
    end: str = "2025-12-31",
    seed: int = 42,
    symbols: Optional[list] = None,
    halt_events: int = 3,
    split_events: int = 2,
) -> BarPanel:
    rng = np.random.default_rng(seed)
    dates = _trading_dates(start, end)
    n = len(dates)
    if symbols is None:
        symbols = [f"6{i:05d}" for i in range(n_symbols)]

    frames: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        # ---- 1) 基础对数价格路径（AR(1) 锚定，长区间有界；横截面分化供动量策略区分）
        mu = rng.normal(0.0004, 0.0005)
        sigma = rng.uniform(0.014, 0.028)
        price0 = float(rng.uniform(8, 80))
        anchor = math.log(price0 * float(rng.uniform(0.8, 4.0)))
        kappa = 0.004
        shocks = rng.normal(0, sigma, n)
        close = np.empty(n)
        log_p = math.log(price0)
        for t in range(n):
            lr = mu + kappa * (anchor + 0.0002 * t - log_p) + shocks[t]
            log_p += float(np.clip(lr, -0.105, 0.105))
            close[t] = math.exp(log_p)

        # ---- 2) 除权除息（真实机制）：除息日起价格进入新 regime（向下跳），
        #         同时记录除权日的“除权参考价”，供引擎按 pre_close 计算涨跌停
        ex_info: Dict[int, Tuple[float, float, float]] = {}
        if split_events > 0 and n > n // 3 + 20:
            for i in sorted(rng.choice(np.arange(n // 3, n - 10), size=split_events, replace=False).tolist()):
                c_prev = float(close[i - 1])
                ratio = float(rng.choice([1.5, 2.0]))
                div = c_prev * 0.01
                ref = (c_prev - div) / ratio          # 除权参考价
                factor = ref / c_prev
                close[i:] *= factor
                ex_info[i] = (ratio, div, ref)
        raw_close = close.copy()

        # ---- 3) 围绕收盘价造 OHLC，然后按 pre_close ±10% 裁剪（含一字板场景）
        open_ = raw_close * (1 + rng.normal(0, 0.004, n))
        high = np.maximum(open_, raw_close) * (1 + np.abs(rng.normal(0, 0.006, n)))
        low = np.minimum(open_, raw_close) * (1 - np.abs(rng.normal(0, 0.006, n)))

        pre_close = np.empty(n)
        pre_close[0] = raw_close[0]
        pre_close[1:] = raw_close[:-1]
        for i, (_r, _d, ref) in ex_info.items():
            pre_close[i] = ref

        limit_up = np.round(pre_close * 1.10, 2)
        limit_down = np.round(pre_close * 0.90, 2)
        sealed_up = rng.random(n) < 0.012
        sealed_down = rng.random(n) < 0.008
        close_f = np.where(sealed_up, limit_up, raw_close)
        close_f = np.where(sealed_down, limit_down, close_f)
        close_f = np.clip(close_f, limit_down, limit_up)
        open_f = np.clip(open_, np.minimum(limit_down, open_), np.maximum(limit_up, open_))
        open_f = np.clip(open_f, limit_down, limit_up)
        high_f = np.clip(np.maximum.reduce([open_f, close_f, high]), limit_down, limit_up)
        low_f = np.clip(np.minimum.reduce([open_f, close_f, low]), limit_down, limit_up)
        # 一字板：OHLC 全等于限价
        one_side = sealed_up | sealed_down
        for col_arr, val in ((open_f, close_f), (high_f, close_f), (low_f, close_f)):
            col_arr[one_side] = val[one_side]

        # ---- 4) 成交量/成交额
        base_amount = rng.uniform(2e8, 6e9) * float(rng.uniform(0.5, 3.0))
        daily_ret = close_f / pre_close - 1.0
        vol_scale = 1 + 6 * np.abs(daily_ret) / sigma
        sealed_mask = one_side
        vol_scale = np.where(sealed_mask, vol_scale * rng.uniform(0.02, 0.2, n), vol_scale)  # 封板缩量
        volume = base_amount * vol_scale * rng.lognormal(0, 0.5, n) / np.maximum(close_f, 0.01)
        volume = np.floor(volume / 100) * 100
        amount = volume * close_f

        df = pd.DataFrame(
            {"open": open_f, "high": high_f, "low": low_f, "close": close_f, "volume": volume, "amount": amount},
            index=dates,
        )

        # ---- 5) 连续停牌（5~20 个交易日；复牌参考价用停牌前收盘 → pre_close 由引擎 ffill 兜底）
        for _ in range(halt_events):
            i0 = int(rng.integers(30, max(31, n - 25)))
            length = int(rng.integers(5, 20))
            df.iloc[i0 : i0 + length, :4] = np.nan
            df.iloc[i0 : i0 + length, 4] = 0.0
            df.iloc[i0 : i0 + length, 5] = 0.0

        df["pre_close"] = np.where(df["close"].notna(), pre_close, np.nan)
        if ex_info:
            df.attrs["splits"] = [(str(dates[i].date()), r, round(d, 4)) for i, (r, d, _ref) in sorted(ex_info.items())]
        df.index.name = "date"
        frames[symbol] = df

    panel = BarPanel.from_wide(frames, metadata={"kind": "synthetic", "seed": seed})
    return panel


def make_demo_panel(seed: int = 42, start: str = "2019-01-02", end: str = "2025-12-31") -> BarPanel:
    """8 个真实代码的合成演示池（含 ETF 与个股，个股代码带 * 标记表示合成）。"""
    symbols = [s for s, _ in DEMO_SYMBOLS]
    panel = make_synthetic_panel(symbols=symbols, seed=seed, start=start, end=end)
    panel.metadata["names"] = dict(DEMO_SYMBOLS)
    panel.metadata["warning"] = "合成数据，仅用于演示引擎与界面，不代表真实行情"
    return panel


def save_demo_data(root: str = "data/cn_a/daily", seed: int = 42) -> list:
    from .loader import save_panel

    return save_panel(make_demo_panel(seed=seed), root, format="parquet")
