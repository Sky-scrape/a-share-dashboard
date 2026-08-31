"""回测配置（dataclass，避免引入 pydantic 依赖）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = ["CostConfig", "SlippageConfig", "RiskConfig", "BacktestConfig", "DEFAULT_TRADING_DAYS"]

DEFAULT_TRADING_DAYS = 244  # A 股年均交易日（2023 年为 242，取 244 作惯例口径）


@dataclass
class CostConfig:
    buy_rate: float = 2.5e-4
    sell_rate: float = 2.5e-4
    min_commission: float = 5.0
    stamp_tax_rate_sell: float = 5e-4
    transfer_fee_rate: float = 1e-5
    other_fee_rate: float = 0.0
    fund_rate: float = 2.5e-4
    fund_min_commission: float = 0.0


@dataclass
class SlippageConfig:
    """model: none | percent | tick | spread

    percent: 成交价偏移 value（0.0005 = 万 5）
    tick:    成交价偏移 value 个最小报价单位
    spread:  以 (high-low)×spread_share 与 price×value 的较小值作为单边滑点
    """

    model: str = "spread"
    value: float = 5e-4
    spread_share: float = 0.5


@dataclass
class RiskConfig:
    #: 单一标的市值占权益上限（0 表示不限）
    max_position_pct: float = 0.0
    #: 组合总仓位上限（0 表示不限）
    max_gross_exposure_pct: float = 1.0
    #: 当日实现+浮动亏损超过权益的该比例时，禁止开新仓（0 表示关闭）
    max_daily_loss_pct: float = 0.0
    #: 权益相对历史高点的回撤超过该比例时清仓并停止交易（0 表示关闭）
    max_drawdown_halt: float = 0.0
    #: 参与率上限：单笔成交量 / 当日成交量
    max_participation_rate: float = 0.05
    #: 单笔最大下单金额（元，0 表示不限）
    max_order_amount: float = 0.0
    #: 禁止买入的标的（如 ST、退市整理、流动性黑名单）
    blacklist: List[str] = field(default_factory=list)
    whitelist: List[str] = field(default_factory=list)


@dataclass
class BacktestConfig:
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_cash: float = 1_000_000.0
    #: 撮合成交价：next_open（T 日信号 → T+1 开盘，推荐）| close（T 日收盘成交，偏乐观）
    execution: str = "next_open"
    cost: CostConfig = field(default_factory=CostConfig)
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    benchmark: Optional[str] = None
    risk_free_rate: float = 0.015
    trading_days_per_year: int = DEFAULT_TRADING_DAYS
    #: 挂单有效期（可尝试撮合的 bar 数）。默认 1 = 一次机会：停牌/限价未触及而
    #: 未成交的订单次日撮合失败即作废；>1 则未成交（cancelled）挂单续挂到期。
    #: rejected（风控/封板）与 partial 剩余部分不续挂。
    order_valid_bars: int = 1
    #: 预热 bar 数：数据窗从 start_date 往前多推 N 根（策略积累历史、建立初始仓位），
    #: 但净值曲线/指标/日期轴只统计 [start_date, end_date] 评估窗。walk-forward 用它
    #: 消除「长回看参数在窗口头部被饿死、折首段强制空仓拍平」的结构性偏差。
    warmup_bars: int = 0
    #: 涨跌停是否完全禁止成交（True 更保守、更贴近实盘）
    block_limit_move: bool = True
    #: 复权方式（数据层用）：hfq | qfq | none
    adjust: str = "qfq"
    #: 是否保留每笔订单明细（record_orders=False 时结果 orders 为空表；
    #: 批量网格/大回测可关省内存，默认开，信号/报告依赖它）
    record_orders: bool = True
    #: 期末是否强制平仓（回测对比需要 True；盘后信号模式用 False 保留持仓+挂单）
    liquidate_on_end: bool = True
    #: 买入现金校验的预估缓冲：按 收盘价×(1+费率)×本系数 缩量，覆盖次日开盘跳空
    cash_demand_pct: float = 1.002
    extra: Dict[str, object] = field(default_factory=dict)

    def normalized(self) -> "BacktestConfig":
        self.execution = (self.execution or "next_open").lower()
        if self.execution not in {"next_open", "close"}:
            raise ValueError("execution 只能是 'next_open' 或 'close'")
        self.adjust = (self.adjust or "qfq").lower()
        return self
