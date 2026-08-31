"""本地行情加载：CSV / Parquet → BarPanel。

推荐目录布局（每标的一个文件）：
    data/cn_a/daily/
        600519.parquet     # 索引=交易日，列=open/high/low/close/volume/amount/pre_close
        000001.parquet
        ...
也支持单文件长表（含 date/symbol 列）。
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

import pandas as pd

from ..core.types import BarPanel, canonical_column
from .adjustment import clean_bars
from . import manifest as man

__all__ = ["load_panel", "load_symbol_frame", "list_symbols", "save_panel"]

_READERS = {
    ".parquet": pd.read_parquet,
    ".pq": pd.read_parquet,
    ".csv": pd.read_csv,
    ".txt": pd.read_csv,
}


def load_symbol_frame(path: str, date_col: Optional[str] = None) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    reader = _READERS.get(ext)
    if reader is None:
        raise ValueError(f"不支持的文件格式: {path}")
    df = reader(path)
    if isinstance(df.index, pd.RangeIndex) or not isinstance(df.index, pd.DatetimeIndex):
        key = date_col
        if key is None:
            for c in df.columns:
                if canonical_column(c) == "date":
                    key = c
                    break
        if key is None:
            raise KeyError(f"{path}: 未找到日期列（date/日期/trade_date）")
        df[key] = pd.to_datetime(df[key])
        df = df.set_index(key)
    df = df.rename(columns={c: (canonical_column(c) or str(c)) for c in df.columns})
    df.index.name = "date"
    return df


def list_symbols(root: str) -> List[str]:
    files = glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True) + glob.glob(
        os.path.join(root, "**", "*.csv"), recursive=True
    )
    return sorted({os.path.splitext(os.path.basename(f))[0] for f in files})


def load_panel(
    root_or_files,
    symbols: Optional[List[str]] = None,
    date_range: Optional[tuple] = None,
    expect_adjust: Optional[str] = None,
) -> BarPanel:
    """从目录（每标的一文件）或文件列表或单长表文件构造 BarPanel。

    expect_adjust：调用方要求的复权口径（qfq/hfq/raw）。目录里存在 `_manifest.json`
    时校验实际口径，不一致的标的进 panel.metadata["manifest_mismatch"]
    （含手工放入、口径未知的文件进 manifest_unknown）——涨跌停判定依赖 pre_close
    口径正确，混入 raw 序列会整体错位且静默，必须入口拦截而非跑完再发现。
    """
    if isinstance(root_or_files, pd.DataFrame):
        return BarPanel.from_long(root_or_files)
    frames: Dict[str, pd.DataFrame] = {}
    if isinstance(root_or_files, str) and os.path.isfile(root_or_files):
        ext = os.path.splitext(root_or_files)[1].lower()
        df = pd.read_parquet(root_or_files) if ext.startswith(".par") else pd.read_csv(root_or_files)
        if "symbol" in {canonical_column(c) for c in df.columns}:
            df = df.rename(columns={c: (canonical_column(c) or str(c)) for c in df.columns})
            return BarPanel.from_long(df)
        raise ValueError("单文件缺少 symbol 列，请使用目录布局（每标的一个文件）")
    if isinstance(root_or_files, str):
        files = sorted(
            glob.glob(os.path.join(root_or_files, "**", "*.parquet"), recursive=True)
            + glob.glob(os.path.join(root_or_files, "**", "*.csv"), recursive=True)
        )
    else:
        files = list(root_or_files)
    for path in files:
        symbol = os.path.splitext(os.path.basename(path))[0]
        if symbols and symbol not in symbols:
            continue
        frames[symbol] = clean_bars(load_symbol_frame(path))
    if not frames:
        raise FileNotFoundError(f"未找到行情文件: {root_or_files}")
    if date_range:
        start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
        frames = {s: d.loc[start:end] for s, d in frames.items()}
    panel = BarPanel.from_wide(frames)
    # manifest 附带与口径校验（仅目录模式有意义）
    try:
        if isinstance(root_or_files, str) and os.path.isdir(root_or_files):
            mdir = root_or_files
        elif files and isinstance(files, list):
            mdir = os.path.dirname(files[0])
        else:
            mdir = None
        if mdir:
            mani = man.read_manifest(mdir)
            loaded_names = {f"{s}.parquet" for s in frames} | {f"{s}.csv" for s in frames}
            loaded_names &= set(mani) if mani else set()
            if mani:
                panel.metadata["manifest"] = {k: mani[k] for k in loaded_names} or None
                if expect_adjust:
                    mism = man.check_adjust({k: mani[k] for k in loaded_names}, expect_adjust)
                    if mism:
                        panel.metadata["manifest_mismatch"] = mism
                    unknown = [k for k in loaded_names if not str(mani[k].get("adjust", ""))]
                    if unknown:
                        panel.metadata["manifest_unknown"] = sorted(unknown)
    except Exception as e:  # manifest 只增值不阻断
        panel.metadata["manifest_error"] = str(e)
    return panel


def save_panel(panel: BarPanel, root: str, format: str = "parquet") -> List[str]:
    """把 BarPanel 按标的拆分落盘。"""
    os.makedirs(root, exist_ok=True)
    out = []
    long = panel.to_long().reset_index()
    for symbol, df in long.groupby("symbol"):
        df = df.set_index("date").drop(columns=["symbol"])
        path = os.path.join(root, f"{symbol}.{format}")
        if format.startswith("par"):
            df.to_parquet(path)
        else:
            df.to_csv(path, encoding="utf-8-sig")
        out.append(path)
    return out
