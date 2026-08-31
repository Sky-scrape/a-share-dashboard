"""确定性微面板构造器（供单测使用）。"""

from __future__ import annotations

from typing import List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from quant_sim.core.types import BarPanel


def make_panel(
    rows_by_symbol: Mapping[str, Sequence[dict]],
    start: str = "2024-01-01",
) -> BarPanel:
    """rows: 每日 dict(open, high, low, close, volume, amount?, pre_close?)。

    日期用工作日序列，行数即天数。pre_close 缺省时由引擎按上一收盘填充。
    """
    n = max(len(v) for v in rows_by_symbol.values())
    dates = pd.bdate_range(start, periods=n)
    frames = {}
    for symbol, rows in rows_by_symbol.items():
        data = {
            "open": [r.get("open", r["close"]) for r in rows],
            "high": [r.get("high", max(r["open"], r["close"])) for r in rows],
            "low": [r.get("low", min(r["open"], r["close"])) for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r.get("volume", 1e9) for r in rows],
            "amount": [r.get("amount", r.get("volume", 1e9) * r["close"]) for r in rows],
        }
        if any("pre_close" in r for r in rows):
            data["pre_close"] = [r.get("pre_close", np.nan) for r in rows]
        idx = pd.DatetimeIndex(dates[: len(rows)])
        df = pd.DataFrame(data, index=idx)
        df.index.name = "date"
        frames[symbol] = df
    return BarPanel.from_wide(frames)


def flat_rows(n: int, price: float = 10.0, volume: float = 1e9) -> List[dict]:
    return [{"open": price, "high": price, "low": price, "close": price, "volume": volume} for _ in range(n)]
