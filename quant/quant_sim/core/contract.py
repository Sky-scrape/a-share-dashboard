"""A 股 / 场内基金交易规则（Contract）。

覆盖 MVP 需要的规则：
  * 最小交易单位 100 股（整手），卖出可一次性清零股
  * 最小报价单位（tick）0.01 元
  * 涨跌停幅度：主板 10%、创业板/科创板 20%、ST 5%、北交所 30%
    （跟踪创业板/科创板指数的 ETF 也是 20%，需要人工传入 override）
  * T+1 资金/托管规则：当日买入不可当日卖出（在 Account 中用 Lot.available 实现）
  * 停牌、退市、上市前的可交易性判断

⚠️ 涨跌停价必须用**除权除息参考价 pre_close** 计算，且按交易所规则四舍五入到 0.01。
回测里 pre_close 用的是复权序列的上一收盘价，因此幅度与真实市场一致。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional

__all__ = ["Contract", "A_SHARE_CONTRACT", "CN_20PCT_FUNDS", "FUND_PREFIXES"]

#: 场内基金（ETF/LOF/REITs）代码段的**单一来源**。
#: 以前这套前缀知识在 contract/hithink 三处手写且已经漂移过（53 段场内基金被
#: 路由到个股分支后静默缺失）；下游一律调用 Contract.is_fund，不得再手抄元组。
FUND_PREFIXES: tuple = ("15", "16", "50", "51", "52", "53", "56", "58")

#: 常见跟踪创业板股票、涨跌幅 20% 的场内基金（策展名单，发现新的直接补）。
#: 不确定的基金宁可留在 10%：只会多拦一笔成交（保守），不会高估可成交性。
CN_20PCT_FUNDS: Dict[str, float] = {
    "159915": 0.20,  # 创业板ETF
    "159949": 0.20,  # 创业板50ETF
    "159952": 0.20,  # 创业板ETF
    "159957": 0.20,  # 创业板ETF
    "159967": 0.20,  # 创业板成长
    "159780": 0.20,  # 创业板人工智能
}

_ST_RE = re.compile(r"(^|[\s*])ST", re.IGNORECASE)


@dataclass
class Contract:
    """一个市场的交易规则集合。默认值即 A 股主板/中小板通用规则。"""

    market: str = "CN_A"
    lot_size: int = 100
    tick_size: float = 0.01
    currency: str = "CNY"
    t_plus_1: bool = True
    allow_short_selling: bool = False
    #: 默认涨跌停幅度（相对 pre_close）
    default_limit_pct: float = 0.10
    #: 按代码前缀匹配的幅度覆盖，按顺序命中；科创板场内基金（588/589）跟随 20%
    limit_rules: tuple = (
        ("^30", 0.20),   # 创业板
        ("^688", 0.20),  # 科创板
        ("^588", 0.20),  # 科创板 ETF/LOF
        ("^589", 0.20),
        ("^8", 0.30),    # 北交所（830/831/833/835/836/837/838/839/870-874）
        ("^43", 0.30),
        ("^92", 0.30),
    )
    st_limit_pct: float = 0.05
    #: 手工覆盖；默认含常见创业板系 20% 基金名单（CN_20PCT_FUNDS），可继续叠加
    limit_overrides: Dict[str, float] = field(default_factory=lambda: dict(CN_20PCT_FUNDS))
    #: 名称含 ST 视为风险警示股（A 股股票适用；ETF 名称一般不含 ST）
    st_applies_to_etf: bool = False
    #: limit_pct 结果缓存（撮合每单至少调 2 次，网格批量下百万次；纯函数结果 memo）。
    #: 改了 limit_overrides/limit_rules 后请 clear_pct_cache()。
    _pct_cache: Dict = field(default_factory=dict, repr=False, compare=False)
    _compiled_rules: Optional[tuple] = field(default=None, repr=False, compare=False)

    def clear_pct_cache(self) -> None:
        self._pct_cache.clear()

    # ------------------------------------------------------------------ 代码
    @staticmethod
    def is_star(symbol: str) -> bool:
        code = symbol.split(".")[0]
        return code.startswith("688") or code.startswith("689")

    @staticmethod
    def is_chinext(symbol: str) -> bool:
        code = symbol.split(".")[0]
        return code.startswith("300") or code.startswith("301") or code.startswith("302")

    @staticmethod
    def is_bse(symbol: str) -> bool:
        code = symbol.split(".")[0]
        return code.startswith(("43", "83", "87", "88", "92"))

    @staticmethod
    def is_fund(symbol: str) -> bool:
        """场内基金（ETF/LOF/REITs）代码段。单一来源：FUND_PREFIXES。"""
        code = str(symbol).split(".")[0]
        return code.startswith(FUND_PREFIXES)

    @staticmethod
    def exchange(symbol: str) -> str:
        code = symbol.split(".")[0]
        if len(symbol.split(".")) > 1 and symbol.split(".")[1].upper() in {"SH", "SS"}:
            return "SH"
        if len(symbol.split(".")) > 1 and symbol.split(".")[1].upper() in {"SZ", "SE"}:
            return "SZ"
        if code.startswith(("6", "5", "9", "7")):
            return "SH"
        return "SZ"

    @staticmethod
    def clean_symbol(symbol: str) -> str:
        """统一成 6 位代码，去掉 .SH/.SZ 后缀。"""
        return str(symbol).split(".")[0]

    # ------------------------------------------------------------------ 数量
    def round_quantity(self, quantity: float, holding: int = 0, closing: bool = False) -> int:
        """下单量取整：向下取到整手；清仓时允许把零股一起卖出。"""
        quantity = int(math.floor(quantity + 1e-9))
        if quantity <= 0:
            return 0
        if closing:
            return int(holding)
        return quantity - quantity % self.lot_size if self.lot_size > 1 else quantity

    # ------------------------------------------------------------------ 涨跌停
    def _rules_compiled(self):
        if self._compiled_rules is None:
            object.__setattr__(self, "_compiled_rules", tuple(
                (re.compile(p), pct) for p, pct in self.limit_rules
            ))
        return self._compiled_rules

    def limit_pct(self, symbol: str, is_st: bool = False) -> float:
        key = (symbol, bool(is_st))
        hit = self._pct_cache.get(key)
        if hit is not None:
            return hit
        pct = self._limit_pct_uncached(symbol, is_st)
        self._pct_cache[key] = pct
        return pct

    def _limit_pct_uncached(self, symbol: str, is_st: bool) -> float:
        code = self.clean_symbol(symbol)
        if code in self.limit_overrides:
            return float(self.limit_overrides[code])
        if code in {self.clean_symbol(k) for k in self.limit_overrides}:
            return float(self.limit_overrides[code])
        fund = self.is_fund(code)
        if is_st and not (fund and not self.st_applies_to_etf):
            return float(self.st_limit_pct)
        for rx, pct in self._rules_compiled():
            if rx.match(code):
                return float(pct)
        return float(self.default_limit_pct)

    def limit_up_price(self, pre_close: float, symbol: str, is_st: bool = False) -> float:
        return self.round_to_tick(pre_close * (1.0 + self.limit_pct(symbol, is_st)))

    def limit_down_price(self, pre_close: float, symbol: str, is_st: bool = False) -> float:
        return self.round_to_tick(pre_close * (1.0 - self.limit_pct(symbol, is_st)))

    def round_to_tick(self, price: float) -> float:
        """价格舍入到最小报价单位的**唯一实现**（撮合层也必须用这里）。

        交易所规则：四舍五入（round-half-up），避开 Python/numpy 的银行家舍入。
        以前 matching.py 自带一份 `round(price, decimals)`（银行家），涨停价与成交价
        在 0.005 边界上口径不一致，可能错一个 tick 直接改变封板判定。
        """
        tick = self.tick_size
        decimals = max(0, int(round(-math.log10(tick)))) if tick < 1 else 0
        scaled = price / tick + 1e-9
        return round(math.floor(scaled + 0.5) * tick, decimals)

    # 兼容旧内部调用名
    _round_to_tick = round_to_tick

    # ------------------------------------------------------------------ 状态
    def is_limit_up(self, bar, pre_close: Optional[float] = None, tol: float = 1e-6) -> bool:
        """一字/封板判定：触及涨停价且收盘仍封住 → 买一无对手，买入按无法成交处理。"""
        pc = bar.pre_close if pre_close is None else pre_close
        if pc <= 0:
            return False
        limit = self.limit_up_price(pc, bar.symbol, bar.is_st)
        return bar.high >= limit - tol and bar.close >= limit - tol

    def is_limit_down(self, bar, pre_close: Optional[float] = None, tol: float = 1e-6) -> bool:
        pc = bar.pre_close if pre_close is None else pre_close
        if pc <= 0:
            return False
        limit = self.limit_down_price(pc, bar.symbol, bar.is_st)
        return bar.low <= limit + tol and bar.close <= limit + tol

    def is_tradable(self, bar) -> bool:
        return bar is not None and bar.is_valid


A_SHARE_CONTRACT = Contract()
