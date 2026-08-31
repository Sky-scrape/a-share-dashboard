"""akshare / 本地数据源适配（网络可用时拉真实 A 股日线）。

用法：
    python -m quant_sim.tools.fetch --symbols 510300 600519 --start 20190101 --end 20251231 --out data/cn_a/daily

说明：
  * 场内基金（5/15/16/5x 开头）走 akshare 的 fund ETF 接口；股票走 stock_zh_a_hist
  * 默认拉**不复权**原始价 + 后复权价两份，引擎读取后复权（qfq 仅用于展示）
    —— 本 MVP 简化：直接拉 qfq 存 raw 列并令 pre_close=shift(1)，复权一致性由 qfq 序列保证；
       正式平台请改为存原始价 + 复权因子（见 docs/design.md 数据层）
  * 网络不可用时抛出带指引的错误，不静默失败
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

import pandas as pd


def _import_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError as e:  # pragma: no cover
        raise ImportError("请先 pip install akshare") from e


def is_fund_code(symbol: str) -> bool:
    code = symbol.split(".")[0]
    return code.startswith(("15", "16", "50", "51", "52", "53", "56", "58"))


def fetch_one(
    symbol: str,
    start: str = "20190101",
    end: str = "20251231",
    adjust: str = "qfq",
    retries: int = 3,
) -> pd.DataFrame:
    ak = _import_akshare()
    code = symbol.split(".")[0]
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            if is_fund_code(code):
                df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust)
            else:
                df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust)
            break
        except Exception as e:  # 网络/接口抖动
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise ConnectionError(
            f"拉取 {code} 失败：{type(last_err).__name__}: {last_err}\n"
            "请检查网络/代理设置，或改用本地 CSV（见 docs/design.md 数据契约）"
        )
    ren = {
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount", "振幅": "amplitude", "涨跌幅": "pct_chg",
        "涨跌额": "change", "换手率": "turnover",
    }
    df = df.rename(columns=ren)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    if "volume" in df.columns:
        df["volume"] = df["volume"] * 100  # akshare 成交量单位为“手”
    return df


def fetch_many(
    symbols: List[str],
    start: str = "20190101",
    end: str = "20251231",
    out_dir: str = "data/cn_a/daily",
    adjust: str = "qfq",
    pause: float = 0.8,
) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for i, symbol in enumerate(symbols):
        code = symbol.split(".")[0]
        try:
            df = fetch_one(code, start=start, end=end, adjust=adjust)
        except Exception as e:
            print(f"[{i + 1}/{len(symbols)}] {code} 失败: {e}")
            continue
        path = os.path.join(out_dir, f"{code}.parquet")
        df.to_parquet(path)
        written.append(path)
        print(f"[{i + 1}/{len(symbols)}] {code} → {path} ({len(df)} rows)")
        time.sleep(pause)
    return written
