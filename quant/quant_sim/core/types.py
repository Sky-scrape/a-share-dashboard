"""领域对象：Bar / BarPanel / Order / Fill / Position / Trade。

设计原则：
1. 引擎内部一律使用**复权价**做信号与撮合，现金与费用单位为人民币元。
2. 所有时间统一为 `pandas.Timestamp`（已归一化到自然日，按 UTC+8 理解）。
3. 数量统一为**股**（场内基金为份），下单量必须为 100 的整数倍（卖光零股除外）。
4. `BarPanel` 采用列式 numpy 数组存储，主循环取当日全市场 bar 为 O(N) 且无 pandas 开销。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "Side",
    "OrderType",
    "OrderStatus",
    "Bar",
    "BarPanel",
    "Order",
    "Fill",
    "Lot",
    "Position",
    "Trade",
    "normalize_date",
]

#: 长表/宽表可接受的列名别名（akshare、通达信、东财、CSV 手工导出常见写法）
COLUMN_ALIASES: Dict[str, Sequence[str]] = {
    "open": ("open", "开盘", "开盘价", "o"),
    "high": ("high", "最高", "最高价", "h"),
    "low": ("low", "最低", "最低价", "l"),
    "close": ("close", "收盘", "收盘价", "c"),
    "volume": ("volume", "成交量", "vol", "v"),
    "amount": ("amount", "成交额", "amt"),
    "pre_close": ("pre_close", "昨收", "前收盘", "prev_close", "preclose"),
    "is_st": ("is_st", "st", "是否ST"),
    "adj_factor": ("adj_factor", "复权因子", "hfq_factor", "qfq_factor"),
    "symbol": ("symbol", "代码", "证券代码", "code", "ts_code"),
    "date": ("date", "日期", "trade_date", "交易日期"),
}

_FIELD_LIST = ["open", "high", "low", "close", "volume", "amount", "pre_close", "is_st"]


def normalize_date(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)
    return ts.normalize()


def canonical_column(name: str) -> Optional[str]:
    key = str(name).strip().lower().replace(" ", "")
    for canonical, aliases in COLUMN_ALIASES.items():
        if key == canonical or key in [a.lower() for a in aliases]:
            return canonical
    return None


# --------------------------------------------------------------------- 行情


@dataclass(frozen=True)
class Bar:
    """单标的单日行情（复权后）。"""

    symbol: str
    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float            # 股
    amount: float = 0.0      # 元
    pre_close: float = 0.0   # 除权除息参考价（复权口径）
    suspended: bool = False
    is_st: bool = False

    @property
    def is_valid(self) -> bool:
        return (not self.suspended) and self.close > 0 and self.volume > 0


class BarPanel:
    """多标的日线容器（列式存储）。

    构造方式：
      * `BarPanel.from_wide({symbol: df})` —— 每标的一个以交易日为索引的表
      * `BarPanel.long_from_frame(df)`      —— MultiIndex(date, symbol) 长表
    """

    def __init__(
        self,
        dates: pd.DatetimeIndex,
        symbols: Sequence[str],
        arrays: Mapping[str, np.ndarray],
        metadata: Optional[dict] = None,
    ) -> None:
        raw = pd.DatetimeIndex(dates)
        # 频率检测（交易日/时间戳解耦的第一步）：日线保留旧的 normalize 行为以兼容
        # 所有现有口径；日内数据绝不静默归一（旧实现会把同一天多根 bar normalize 后
        # 去重成最后一根，分钟线在入口层就被悄悄截掉）。日内面板原样保留时间戳，
        # 由引擎显式拒绝，而不是给出虚高的 T+0 结果。
        self.freq = "daily" if not pd.DatetimeIndex(raw.normalize()).duplicated().any() else "intraday"
        self.dates = raw.normalize() if self.freq == "daily" else raw
        self.symbols = list(symbols)
        self.n_dates = len(self.dates)
        self.n_symbols = len(self.symbols)
        self.arrays = {k: np.asarray(v, dtype=float if k != "is_st" else bool) for k, v in arrays.items()}
        self._col = {s: i for i, s in enumerate(self.symbols)}
        self._row = {d: i for i, d in enumerate(self.dates)}
        # 每个标的自身的有行情日期序列（用于 history，天然规避未来数据）
        self._symbol_dates: Dict[str, pd.DatetimeIndex] = {}
        self._build_symbol_dates(self.arrays["close"])
        self.metadata = dict(metadata or {})
        self.metadata.setdefault("freq", self.freq)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def _from_frame(cls, df: pd.DataFrame, metadata: dict) -> "BarPanel":
        """df: MultiIndex(date, symbol)，列为 canonical 字段。

        注意：这里**不再**无条件 normalize 日期层（旧实现会把同日内多根分钟 bar
        normalize 后当作重复行丢弃）；日线在 __init__ 里统一 normalize。
        """
        df = df.copy()
        date_level = pd.DatetimeIndex(df.index.get_level_values("date"))
        symbol_level = df.index.get_level_values("symbol").astype(str)
        df.index = pd.MultiIndex.from_arrays([date_level, symbol_level], names=["date", "symbol"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        dates = pd.DatetimeIndex(sorted(df.index.get_level_values("date").unique()))
        symbols = sorted(df.index.get_level_values("symbol").unique())
        rows = np.asarray(df.index.get_level_values("date").map({d: i for i, d in enumerate(dates)}), dtype=int)
        cols = np.asarray(df.index.get_level_values("symbol").map({s: i for i, s in enumerate(symbols)}), dtype=int)
        arrays: Dict[str, np.ndarray] = {}
        for field_name in _FIELD_LIST:
            if field_name == "is_st":
                mat = np.zeros((len(dates), len(symbols)), dtype=bool)
                if field_name in df.columns:
                    values = pd.to_numeric(df[field_name], errors="coerce").fillna(0).to_numpy(dtype=float)
                    mat[rows, cols] = values.astype(bool)
            else:
                # volume / amount 缺失 → NaN，语义为“停牌或无成交”
                mat = np.full((len(dates), len(symbols)), np.nan)
                if field_name in df.columns:
                    values = pd.to_numeric(df[field_name], errors="coerce").to_numpy(dtype=float)
                    mat[rows, cols] = values
                elif field_name == "amount":
                    mat[:] = 0.0
            arrays[field_name] = mat
        # pre_close 缺失时用上一交易日收盘（前复权口径下等价于真实涨跌幅基准）
        pc = arrays.get("pre_close")
        close = arrays["close"]
        if pc is None:
            pc = np.full_like(close, np.nan)
        empty = np.isnan(pc).all()
        if empty:
            pc[:, :] = np.nan
            for j in range(close.shape[1]):
                series = pd.Series(close[:, j])
                pc[:, j] = series.shift(1).to_numpy()
        else:
            for j in range(close.shape[1]):
                mask = np.isnan(pc[:, j]) & ~np.isnan(close[:, j])
                series = pd.Series(close[:, j])
                pc[mask, j] = series.shift(1).to_numpy()[mask]
        arrays["pre_close"] = pc
        panel = cls(dates, symbols, arrays, metadata)
        panel._build_symbol_dates(close)
        return panel

    def _build_symbol_dates(self, close: np.ndarray) -> None:
        for j, symbol in enumerate(self.symbols):
            mask = ~np.isnan(close[:, j])
            self._symbol_dates[symbol] = self.dates[mask]

    @classmethod
    def from_long(cls, df: pd.DataFrame, metadata: Optional[dict] = None) -> "BarPanel":
        d = df.copy()
        if not isinstance(d.index, pd.MultiIndex):
            cols = {canonical_column(c): c for c in d.columns}
            if "date" not in cols or "symbol" not in cols:
                raise KeyError("长表需要 date/日期 与 symbol/代码 列，或使用 MultiIndex(date, symbol)")
            d[cols["date"]] = pd.to_datetime(d[cols["date"]])
            d = d.set_index([cols["date"], cols["symbol"]])
        d.index = pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex(d.index.get_level_values(0)), d.index.get_level_values(1).astype(str)],
            names=["date", "symbol"],
        )
        d = d.rename(columns={c: (canonical_column(c) or str(c)) for c in d.columns})
        return cls._from_frame(d, {"source": "from_long", **(metadata or {})})

    @classmethod
    def from_wide(
        cls,
        frames: Mapping[str, pd.DataFrame],
        metadata: Optional[dict] = None,
    ) -> "BarPanel":
        records = []
        for symbol, raw in frames.items():
            df = raw.copy()
            if not isinstance(df.index, pd.DatetimeIndex):
                key = None
                for c in df.columns:
                    if canonical_column(c) == "date":
                        key = c
                        break
                if key is None:
                    raise KeyError(f"{symbol}: 索引不是日期，且未找到 date/日期 列")
                df[key] = pd.to_datetime(df[key])
                df = df.set_index(key)
            df.index = pd.DatetimeIndex(df.index)
            # 不在此处 normalize/去重：频率判定与 normalize 统一交给 __init__；
            # 重复行去重在 _from_frame 按原始时间戳进行，避免日内 bar 被静默截掉。
            df = df.sort_index()
            df = df.rename(columns={c: (canonical_column(c) or str(c)) for c in df.columns})
            missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
            if missing:
                raise KeyError(f"{symbol}: 缺少必需列 {missing}")
            df = df.copy()
            df["symbol"] = str(symbol)
            df["date"] = df.index
            records.append(df.reset_index(drop=True))
        if not records:
            raise ValueError("frames 为空")
        long = pd.concat(records, ignore_index=True).set_index(["date", "symbol"])
        return cls._from_frame(long, {"source": "from_wide", **(metadata or {})})

    # ------------------------------------------------------------------ 访问
    @property
    def active(self) -> np.ndarray:
        key = "_active"
        cache = self.metadata.get(key)
        if cache is None:
            close = self.arrays["close"]
            volume = self.arrays["volume"]
            cache = ~np.isnan(close) & (np.nan_to_num(volume, nan=0.0) > 0)
            self.metadata[key] = cache
        return cache

    def date_index(self, date) -> int:
        d = normalize_date(date)
        i = self._row.get(d)
        if i is None:
            i = int(self.dates.searchsorted(d, side="right")) - 1
            if i < 0 or self.dates[i] != d:
                raise KeyError(f"日期 {d.date()} 不在行情区间内")
        return i

    def bars(self, date) -> Dict[str, Bar]:
        """某交易日全部有效标的的 Bar（报告导出/测试用；引擎主循环走 bars_view 懒构造）。"""
        i = self.date_index(date)
        a = self.arrays
        out: Dict[str, Bar] = {}
        date_ts = self.dates[i]
        for j, symbol in enumerate(self.symbols):
            close = a["close"][i, j]
            if np.isnan(close):
                continue
            volume = a["volume"][i, j]
            volume = 0.0 if np.isnan(volume) else float(volume)
            pre_close = a["pre_close"][i, j]
            out[symbol] = Bar(
                symbol=symbol,
                date=date_ts,
                open=float(a["open"][i, j]),
                high=float(a["high"][i, j]),
                low=float(a["low"][i, j]),
                close=float(close),
                volume=volume,
                amount=float(a["amount"][i, j]),
                pre_close=float(pre_close) if not np.isnan(pre_close) else float(close),
                suspended=volume <= 0,
                is_st=bool(a["is_st"][i, j]),
            )
        return out

    def bars_view(self, i: int) -> "BarsView":
        """当日 Bar 的**懒构造只读视图**：500 标的×数千日的回测里，策略/撮合每日
        只摸几只，不应物化 500 个 Bar 对象（实测全量构造占主循环一半耗时）。"""
        return BarsView(self, i)

    def price_at(self, i: int, symbol: str, field: str = "close") -> Optional[float]:
        j = self._col.get(symbol)
        if j is None:
            return None
        value = self.arrays[field][i, j]
        return None if np.isnan(value) else float(value)

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self._col

    def history(self, symbol: str, end, length: Optional[int] = None) -> Optional[pd.DataFrame]:
        """截至 end（含）该标的自身有行情的历史；停牌日自然跳过。"""
        dates = self._symbol_dates.get(symbol)
        if dates is None or len(dates) == 0:
            return None
        end = normalize_date(end)
        pos = int(dates.searchsorted(end, side="right")) - 1
        if pos < 0:
            return None
        start = 0 if length is None else max(0, pos - length + 1)
        sub = dates[start : pos + 1]
        rows = np.fromiter((self._row[d] for d in sub), dtype=int, count=len(sub))
        j = self._col[symbol]
        data = {f: self.arrays[f][rows, j] for f in ["open", "high", "low", "close", "volume", "amount", "pre_close"]}
        df = pd.DataFrame(data, index=pd.DatetimeIndex(sub))
        df.index.name = "date"
        return df

    def trailing_return(self, field: str, periods: int) -> np.ndarray:
        """向量化 N 日涨跌幅矩阵（仅使用 T-n 与 T 的价格，无前视）。"""
        close = self.arrays["close"]
        prior = np.roll(close, periods, axis=0)
        prior[:periods, :] = np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            return close / prior - 1.0

    def to_long(self) -> pd.DataFrame:
        frames = []
        idx = pd.MultiIndex.from_product([self.dates, self.symbols], names=["date", "symbol"])
        for f in _FIELD_LIST:
            frames.append(pd.Series(self.arrays[f].ravel(), index=idx, name=f))
        return pd.concat(frames, axis=1).dropna(subset=["close"])


# --------------------------------------------------------------------- 交易对象


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"      # 已提交，等待下一根 bar 撮合
    FILLED = "filled"        # 全部成交
    PARTIAL = "partial"      # 部分成交（剩余当日作废）
    REJECTED = "rejected"    # 拒单（风控/涨跌停/资金不足等）
    CANCELLED = "cancelled"  # 过期未成交自动撤单


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    reason: str = ""
    id: int = -1
    created_date: Optional[pd.Timestamp] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    reject_reason: str = ""

    def __post_init__(self) -> None:
        self.side = Side(self.side)
        self.order_type = OrderType(self.order_type)
        self.quantity = int(self.quantity)


@dataclass
class Fill:
    order_id: int
    symbol: str
    side: Side
    date: pd.Timestamp
    quantity: int
    price: float             # 含滑点的成交价（复权价）
    raw_price: float         # 未含滑点的市场价
    gross_amount: float      # 成交金额（元）
    commission: float
    stamp_tax: float
    transfer_fee: float
    other_fee: float
    slippage_cost: float
    cash_flow: float         # 买入为负、卖出为正
    reason: str = ""

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee + self.other_fee


@dataclass
class Lot:
    """建仓批次：FIFO 配对 + T+1 可卖数量控制。"""

    date: pd.Timestamp
    price: float
    quantity: int
    cost: float = 0.0
    available: Optional[int] = None
    order_id: int = -1

    def __post_init__(self) -> None:
        if self.available is None:
            self.available = self.quantity
        self.available = min(int(self.available), int(self.quantity))


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    lots: List[Lot] = field(default_factory=list)

    @property
    def available(self) -> int:
        return int(sum(l.available for l in self.lots))

    @property
    def locked(self) -> int:
        return int(self.quantity - self.available)

    @property
    def total_cost(self) -> float:
        return float(sum(l.price * l.quantity + l.cost for l in self.lots))

    @property
    def avg_cost(self) -> float:
        return self.total_cost / self.quantity if self.quantity else 0.0

    def __bool__(self) -> bool:
        return self.quantity > 0


@dataclass
class Trade:
    """一次 FIFO 配对（开仓→平仓），用于胜率、盈亏比、持仓周期统计。"""

    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    cost: float
    net_pnl: float
    return_pct: float
    holding_days: int
    holding_bars: int
    entry_order_id: int
    exit_order_id: int


class BarsView(Mapping):
    """某交易日 Bar 的懒构造只读视图（Mapping 接口，兼容全量 Bar dict 的用法）。

    存在的意义：主循环每日只应为用户真正查询的标的付 Bar 构造成本。
    keys/__iter__/valid_symbols/fill_last_prices 全部走 numpy 掩码，不构造 Bar；
    只有 __getitem__ 命中才实例化单个 Bar。撮合/风控/ctx.price 都只 .get 少数标的，
    500 标的全市场回测因此从「每日物化 500 个对象」降到「每日几只」。
    """

    __slots__ = ("_panel", "_i", "_cache", "_keys", "_valid")

    def __init__(self, panel: "BarPanel", i: int) -> None:
        self._panel = panel
        self._i = i
        self._cache: Dict[str, Bar] = {}
        self._keys: Optional[List[str]] = None
        self._valid: Optional[List[str]] = None

    def _all_keys(self) -> List[str]:
        if self._keys is None:
            close = self._panel.arrays["close"][self._i]
            idx = np.flatnonzero(~np.isnan(close))
            self._keys = [self._panel.symbols[j] for j in idx]
        return self._keys

    def valid_symbols(self) -> List[str]:
        """当日 is_valid（有行情且非停牌）的标的——ctx.universe 快速路径。"""
        if self._valid is None:
            active = self._panel.active[self._i]
            idx = np.flatnonzero(active)
            self._valid = [self._panel.symbols[j] for j in idx]
        return self._valid

    def fill_last_prices(self, dst: Dict[str, float]) -> None:
        """更新有效标的收盘价到 dst（引擎 _update_last_prices 快速路径，不构造 Bar）。"""
        a = self._panel.arrays
        close = a["close"][self._i]
        idx = np.flatnonzero(self._panel.active[self._i])
        for j in idx:
            dst[self._panel.symbols[j]] = float(close[j])

    def __getitem__(self, symbol: str) -> Bar:
        bar = self._cache.get(symbol)
        if bar is not None:
            return bar
        j = self._panel._col.get(symbol)
        if j is None:
            raise KeyError(symbol)
        a = self._panel.arrays
        i = self._i
        close = a["close"][i, j]
        if np.isnan(close):
            raise KeyError(symbol)
        volume = a["volume"][i, j]
        volume = 0.0 if np.isnan(volume) else float(volume)
        pre_close = a["pre_close"][i, j]
        bar = Bar(
            symbol=symbol,
            date=self._panel.dates[i],
            open=float(a["open"][i, j]),
            high=float(a["high"][i, j]),
            low=float(a["low"][i, j]),
            close=float(close),
            volume=volume,
            amount=float(a["amount"][i, j]),
            pre_close=float(pre_close) if not np.isnan(pre_close) else float(close),
            suspended=volume <= 0,
            is_st=bool(a["is_st"][i, j]),
        )
        self._cache[symbol] = bar
        return bar

    def __iter__(self):
        return iter(self._all_keys())

    def __len__(self):
        return len(self._all_keys())
