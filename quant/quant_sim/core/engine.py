"""事件驱动回测引擎（A 股日线）。

主循环每个交易日严格按以下顺序执行，**杜绝未来函数**：

    1. 撮合昨日提交的委托（next_open：以今日开盘价成交）
    2. T+1 解锁（昨日及以前买入的份额今日可卖）
    3. 记录当日期初权益，跑风控熔断
    4. 调用 strategy.on_bar(ctx) —— 策略只能看到截至**昨日**的成交与截至**今日收盘**的行情
    5. 提交委托：close 模式下立即以今日收盘价撮合；next_open 模式下进入队列等明日撮合
    6. 记录当日期末权益、持仓快照

关键取舍：`execution="next_open"` 是默认且推荐的（T 日信号 T+1 开盘成交）；
`execution="close"` 相当于假设能在收盘集合竞价按收盘价成交，结果偏乐观，仅用于快速对比。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Union

import numpy as np
import pandas as pd

from .account import Account
from .config import BacktestConfig
from .contract import Contract
from .cost import CostModel
from .matching import MatchingEngine
from .risk import RiskEvent, RiskManager
from .strategy_base import Strategy
from .types import Bar, BarPanel, Fill, Lot, Order, OrderStatus, OrderType, Position, Side, Trade

__all__ = ["BacktestEngine", "BacktestResult", "Context", "run_backtest"]


# =====================================================================================
# 策略上下文
# =====================================================================================
class Context:
    """策略唯一可见的世界。所有查询都限定在截至当前 bar 的信息集内。"""

    def __init__(self, engine: "BacktestEngine", date: pd.Timestamp, bars: Dict[str, Bar]):
        self._e = engine
        self.date = date
        self._bars = bars
        self.last_bar_index = engine.dates.get_indexer([date])[0]

    # ---------------------------------------------------------------- 行情
    @property
    def symbols(self) -> List[str]:
        return self._e.panel.symbols

    @property
    def universe(self) -> List[str]:
        """当日有有效行情的标的（BarsView 快速路径，不物化 Bar）。"""
        vs = getattr(self._bars, "valid_symbols", None)
        if vs is not None:
            return vs()
        return [s for s, b in self._bars.items() if b.is_valid]

    def bar(self, symbol: str) -> Optional[Bar]:
        return self._bars.get(symbol)

    def price(self, symbol: str, field_: str = "close") -> Optional[float]:
        bar = self._bars.get(symbol)
        return getattr(bar, field_) if bar is not None else None

    def history(self, symbol: str, length: Optional[int] = None, to_date=None) -> Optional[pd.DataFrame]:
        """截至 `to_date`（默认为**昨日收盘**，即不含当日未完成的行情）的历史 bar。

        ⚠️ 默认 to_date=昨日：信号在当日收盘生成、次日成交，因此指标不应包含当日收盘价，
        除非你明确选择 `execution='close'`。需要当日行情请显式传 to_date=ctx.date。
        """
        end = to_date if to_date is not None else self._e.prev_date(self.date)
        if end is None:
            return None
        return self._e.panel.history(symbol, end, length)

    # ---------------------------------------------------------------- 账户
    @property
    def cash(self) -> float:
        return self._e.account.cash

    @property
    def equity(self) -> float:
        return self._e.equity(self.date)

    @property
    def positions(self) -> Dict[str, Position]:
        return {s: p for s, p in self._e.account.positions.items() if p.quantity > 0}

    def position(self, symbol: str) -> Position:
        return self._e.account.position(symbol)

    def holding(self, symbol: str) -> int:
        return self._e.account.position(symbol).quantity

    def available(self, symbol: str) -> int:
        return self._e.account.position(symbol).available

    @property
    def pending(self) -> List[Order]:
        return list(self._e.pending)

    @property
    def trades(self) -> List[Trade]:
        return self._e.account.trades

    @property
    def fills(self) -> List[Fill]:
        return self._e.account.fills

    # ---------------------------------------------------------------- 下单
    def buy(self, symbol: str, quantity: float = 0, percent: float = 0.0, limit: Optional[float] = None, reason: str = "") -> Order:
        return self._submit(symbol, Side.BUY, quantity, percent, limit, reason)

    def sell(self, symbol: str, quantity: float = 0, percent: float = 0.0, limit: Optional[float] = None, reason: str = "") -> Order:
        return self._submit(symbol, Side.SELL, quantity, percent, limit, reason)

    def target_percent(self, symbol: str, target_percent: float, reason: str = "") -> List[Order]:
        """把某标的调整到目标权益占比（自动算差额、整手取整、可卖优先）。"""
        return self._e.rebalance_to(self, {symbol: target_percent}, reason=reason)

    def rebalance(self, targets: Mapping[str, float], reason: str = "") -> List[Order]:
        """一次性给出目标权重字典（{标的: 权益占比}），引擎平掉多余、补足不足。"""
        return self._e.rebalance_to(self, targets, reason=reason)

    def flat(self, reason: str = "force flat") -> List[Order]:
        return self._e.rebalance_to(self, {}, reason=reason)

    def _submit(self, symbol: str, side: Side, quantity: float, percent: float, limit, reason) -> Order:
        qty = int(quantity or 0)
        if percent:
            px = self.price(symbol) or self._e.last_price(symbol)
            if not px:
                return self._e.record_rejected_order(symbol, side, 0, "无行情，无法按金额换算股数")
            qty = int(self._e.account.cash * percent / px) if side is Side.BUY else int(self.holding(symbol) * percent)
        return self._e.submit_order(Order(symbol=symbol, side=side, quantity=qty, limit_price=limit, reason=reason))

    # ---------------------------------------------------------------- 其它
    def should_rebalance(self, every_n_bars: int = 1) -> bool:
        idx = self.last_bar_index
        if idx == 0:
            return True
        if every_n_bars <= 1:
            return True
        return (idx % every_n_bars) == 0

    def log(self, message: str) -> None:
        self._e.logs.append(f"[{self.date.date()}] {message}")


# =====================================================================================
# 结果对象
# =====================================================================================
@dataclass
class BacktestResult:
    config: BacktestConfig
    dates: pd.DatetimeIndex
    symbols: List[str]
    equity: pd.Series
    cash: pd.Series
    holdings_value: pd.Series
    positions: pd.DataFrame            # 日期 × 标的 持仓股数
    weights: pd.DataFrame              # 日期 × 标的 仓位占比
    fills: pd.DataFrame
    orders: pd.DataFrame
    trades: pd.DataFrame
    metrics: Dict[str, float]
    metrics_df: pd.DataFrame
    risk_events: List[RiskEvent]
    logs: List[str] = field(default_factory=list)
    benchmark: Optional[pd.Series] = None
    panel: Optional[BarPanel] = None
    force_liquidated: bool = False
    #: 含预热段的完整净值（预热期为窗口之前的连续运行段）；warmup_bars=0 时等于 equity。
    #: walk-forward 拼接 OOS 用它，避免窗口首日相对初始资金的假收益。
    equity_full: Optional[pd.Series] = None

    def summary(self) -> str:
        from ..report.formatter import format_summary

        return format_summary(self.metrics)

    def save(self, out_dir: str = "results", name: str = "backtest", formats=("csv", "json", "html"), data_note: str = "") -> List[str]:
        from ..report.exporter import save_result

        return save_result(self, out_dir=out_dir, name=name, formats=formats, data_note=data_note)

    @property
    def drawdown(self) -> pd.Series:
        from ..metrics.performance import drawdown_series

        return drawdown_series(self.equity)


# =====================================================================================
# 引擎
# =====================================================================================
class BacktestEngine:
    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        contract: Optional[Contract] = None,
        cost_model: Optional[CostModel] = None,
        risk_manager: Optional[RiskManager] = None,
    ) -> None:
        self.config = (config or BacktestConfig()).normalized()
        self.contract = contract or Contract()
        self.cost_model = cost_model or CostModel.from_config(self.config.cost, self.config.slippage)
        self.risk = risk_manager or RiskManager(self.config.risk, self.contract, self.cost_model)
        self.matcher = MatchingEngine(
            self.cost_model,
            self.contract,
            execution=self.config.execution,
            max_participation_rate=self.config.risk.max_participation_rate,
            block_limit_move=self.config.block_limit_move,
        )
        # 期末强平专用撮合器：复用撮合语义（滑点/涨跌停/参与率），但价格锚点固定为
        # 收盘=最后价——期末已无「次日开盘」，next_open 模式的主撮合器会错锚到末日开盘。
        self._close_matcher = MatchingEngine(
            self.cost_model,
            self.contract,
            execution="close",
            max_participation_rate=self.config.risk.max_participation_rate,
            block_limit_move=self.config.block_limit_move,
        )

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        strategy: Strategy,
        data: Union[BarPanel, pd.DataFrame, Mapping[str, pd.DataFrame]],
        lite: bool = False,
    ) -> BacktestResult:
        """lite=True：只产净值/指标，不构 positions/weights/成交明细表（网格/WF 批量回测专用）。"""
        panel = self._as_panel(data)
        panel, dates, eval_dates = self._slice(panel)
        if panel.metadata.get("freq") == "intraday":
            # fail loudly, not silently：撮合价格锚点只有 {next_open, close} 两个日线假设，
            # 分钟线接入前必须完成 ExecutionModel 重构（日内路径撮合/会话解锁语义）。
            raise NotImplementedError(
                "日内（分钟/tick）数据尚未支持：面板按频率解耦后，撮合层价格锚点仍为日线假设（P2 ExecutionModel）。"
                "若继续跑会把 T+1 解锁变成日内 T+0，回测结果虚高且无报错——引擎选择直接拒绝。"
            )
        self.panel = panel
        self.dates = dates
        self.current_date = dates[0]
        self.current_bars: Dict[str, Bar] = {}
        self.account = Account(initial_cash=self.config.initial_cash, contract=self.contract)
        self.orders: List[Order] = []
        self.pending: List[Order] = []
        self.logs: List[str] = []
        self.risk_events: List[RiskEvent] = []
        self._order_seq = 0
        self._order_attempts: Dict[int, int] = {}  # order_valid_bars>1 时的撮合尝试计数
        self._session: Optional[pd.Timestamp] = None  # 当前日历交易日（T+1 解锁以它为准，非 bar 数）
        self._last_prices: Dict[str, float] = {}
        self._halted = False
        self._buy_forbidden = False
        self._day_start_equity = self.config.initial_cash
        self._peak_equity = self.config.initial_cash
        # 同根 bar 下单批次内的投影缓存（_pending_projection 首扫 + submit_order 增量维护）
        self._proj: Optional[List] = None
        self._holdings_total: Optional[float] = None
        self._holdings_parts: Dict[str, float] = {}

        rows: List[dict] = []
        pos_rows: List[Dict[str, int]] = []
        symbols = panel.symbols
        strategy.on_start(self._ctx(dates[0], panel.bars_view(0)))

        for i, date in enumerate(dates):
            bars = panel.bars_view(i)
            self.current_date = date
            self.current_bars = bars
            self._update_last_prices(bars)
            # 新的一根 bar：上一批次（同 bar 提交）的投影缓存全部失效
            self._proj = None
            self._holdings_total = None

            # 1) T+1 解锁：仅在**日历交易日变更**时触发（不是每根 bar）。
            #    日线面板每天变更，行为与旧版完全一致；将来支持日内后，
            #    同一天多根 bar 不会再把当日买入份额分钟级解锁成 T+0。
            day = pd.Timestamp(date).normalize()
            if self._session != day:
                self._session = day
                self.account.on_new_day(date)

            # 2) 撮合昨日委托（next_open：以今日开盘价成交）
            if self.pending and self.config.execution == "next_open":
                self._process_pending(bars, date)

            # 3) 期初权益 + 熔断
            equity_open = self.account.equity(self._last_prices)
            events = self.risk.check_circuit_breaker(date, equity_open, self._peak_equity, self._day_start_equity, self.config.initial_cash)
            for ev in events:
                self.risk_events.append(ev)
                if ev.action == "halt":
                    self._halted = True
                    ctx = self._ctx(date, bars)
                    ctx.flat(reason="max_drawdown_halt")
                    if self.config.execution == "next_open":
                        self._process_pending(bars, date)
                elif ev.action == "reject_buy":
                    self._buy_forbidden = True

            # 4) 策略
            ctx = self._ctx(date, bars)
            strategy.on_bar(ctx)

            # 5) close 模式当日撮合
            if self.pending and self.config.execution == "close":
                self._process_pending(bars, date)

            # 6) 期末快照
            price_map = dict(self._last_prices)
            eq = self.account.equity(price_map)
            self._peak_equity = max(self._peak_equity, eq)
            holdings = self.account.holdings_value(price_map)
            rows.append({"date": date, "cash": self.account.cash, "holdings_value": holdings, "equity": eq})
            if not lite:
                # lite（网格/WF 批量）最终丢弃 T×S 持仓表：跳过全符号逐只查询，省 O(S)/bar
                pos_rows.append({s: self.account.position(s).quantity for s in symbols})
            self._day_start_equity = eq
            if self._buy_forbidden:
                self._buy_forbidden = False  # 仅当日有效

        strategy.on_finish(self._ctx(dates[-1], panel.bars_view(len(dates) - 1)))

        force = self._force_liquidate(dates[-1], panel) if self.config.liquidate_on_end else False
        frame_full = pd.DataFrame(rows).set_index("date")
        if force:
            # 期末仍有未平仓持仓（跌停封板/停牌/T+1 当日买入）时按最后价实估，
            # 不再无条件把 holdings_value 拍成 0（净值与持仓保持会计一致）。
            frame_full.loc[frame_full.index[-1], "cash"] = self.account.cash
            frame_full.loc[frame_full.index[-1], "holdings_value"] = self.account.holdings_value(self._last_prices)
            frame_full.loc[frame_full.index[-1], "equity"] = self.account.equity(self._last_prices)
        # 评估窗切分：预热段连续运行（建立仓位），但曲线/成交/指标只统计 [start, end]。
        emask = frame_full.index >= eval_dates[0]
        has_warmup = not bool(emask.all())
        frame = frame_full[emask]

        from ..metrics.performance import compute_metrics, make_metrics_dataframe

        benchmark_full = None
        if self.config.benchmark and panel.has_symbol(self.config.benchmark):
            benchmark_full = pd.Series(
                [panel.price_at(i, self.config.benchmark) for i in range(len(dates))], index=dates, name=self.config.benchmark
            ).ffill()
        benchmark = benchmark_full[emask] if benchmark_full is not None else None
        acct_view = self._window_account(eval_dates[0]) if has_warmup else self.account
        metrics = compute_metrics(frame, acct_view.trades, acct_view, config=self.config, benchmark=benchmark)

        if lite:
            # 批量研究只消费 metrics + 净值曲线；T×S 的持仓/权重/明细表与三张明细 DataFrame
            # 是网格/WF 的主要开销，全部跳过。
            empty = pd.DataFrame()
            return BacktestResult(
                config=self.config, dates=eval_dates, symbols=symbols,
                equity=frame["equity"], cash=frame["cash"], holdings_value=frame["holdings_value"],
                positions=empty, weights=empty, fills=empty, orders=empty, trades=empty,
                metrics=metrics, metrics_df=empty, risk_events=self.risk_events, logs=self.logs,
                benchmark=benchmark, panel=panel, force_liquidated=force, equity_full=frame_full["equity"],
            )

        positions_full = pd.DataFrame(pos_rows, index=dates).fillna(0).astype(int)
        # 向量化：旧实现对每个 (日,标的) 调一次 price_at（T×S Python 循环），直接拿矩阵。
        close_mat = pd.DataFrame(
            np.nan_to_num(panel.arrays["close"], nan=0.0), index=dates, columns=symbols,
        )
        positions = positions_full[emask]
        market_values = positions * close_mat[emask]
        weights = market_values.div(frame["equity"].replace(0, np.nan), axis=0).fillna(0.0)
        metrics_df = make_metrics_dataframe(metrics)

        return BacktestResult(
            config=self.config,
            dates=eval_dates,
            symbols=symbols,
            equity=frame["equity"],
            cash=frame["cash"],
            holdings_value=frame["holdings_value"],
            positions=positions,
            weights=weights,
            fills=pd.DataFrame([self._fill_row(f) for f in acct_view.fills]),
            orders=pd.DataFrame([self._order_row(o) for o in self.orders]),
            trades=pd.DataFrame([self._trade_row(t) for t in acct_view.trades]),
            metrics=metrics,
            metrics_df=metrics_df,
            risk_events=self.risk_events,
            logs=self.logs,
            benchmark=benchmark,
            panel=panel,
            force_liquidated=force,
            equity_full=frame_full["equity"],
        )

    def _window_account(self, eval_start):
        """账户的评估窗视图：预热段的成交/已实现交易不计入窗口指标（费用、笔数、胜率）。"""
        from dataclasses import replace

        acct = self.account
        fills = [f for f in acct.fills if f.date >= eval_start]
        trades = [t for t in acct.trades if t.exit_date >= eval_start]
        return replace(
            acct,
            fills=fills,
            trades=trades,
            total_commission=float(sum(f.commission for f in fills)),
            total_stamp_tax=float(sum(f.stamp_tax for f in fills)),
            total_transfer_fee=float(sum(f.transfer_fee for f in fills)),
            total_other_fee=float(sum(f.other_fee for f in fills)),
            total_slippage=float(sum(f.slippage_cost for f in fills)),
            realized_pnl=float(sum(t.net_pnl for t in trades)),
        )

    # ------------------------------------------------------------------ 内部
    def _as_panel(self, data) -> BarPanel:
        if isinstance(data, BarPanel):
            return data
        if isinstance(data, Mapping):
            return BarPanel.from_wide(data)
        if isinstance(data, pd.DataFrame):
            return BarPanel.from_long(data)
        raise TypeError(f"不支持的数据类型: {type(data)}")

    def _slice(self, panel: BarPanel):
        """返回 (数据窗 panel, 数据窗日期, 评估窗日期)。

        warmup_bars>0 时数据窗从 start_date 往前多推 N 根：策略在预热段真实运行
        （积累历史、建立初始仓位），但净值曲线/成交/指标只统计评估窗——消除
        walk-forward「长回看参数在窗口头部被饿死、折首段强制空仓拍平」的结构性偏差。

        切窗缓存挂在 panel.metadata（键 "_slice_cache"）：网格/WF 的全部变体共享
        同一份 panel 而引擎每变体新建，同 (start,end,warmup) 的整块字段矩阵复制
        只做一次。panel 存活即缓存存活，不跨 panel 泄漏。
        """
        all_dates = panel.dates
        eval_dates = all_dates
        if self.config.start_date:
            eval_dates = eval_dates[eval_dates >= pd.Timestamp(self.config.start_date).normalize()]
        if self.config.end_date:
            eval_dates = eval_dates[eval_dates <= pd.Timestamp(self.config.end_date).normalize()]
        if len(eval_dates) < 2:
            raise ValueError("回测区间内交易日不足 2 天，请检查 start_date/end_date 与数据范围")
        warm = int(getattr(self.config, "warmup_bars", 0) or 0)
        cache = panel.metadata.setdefault("_slice_cache", {})
        key = (eval_dates[0], eval_dates[-1], len(eval_dates), warm)
        hit = cache.get(key)
        if hit is not None:
            return hit
        data_dates = eval_dates
        if warm > 0:
            i0 = int(all_dates.searchsorted(eval_dates[0]))
            # 只往前推预热段，**右端必须截在评估窗末尾**：旧写法 all_dates[i0-warm:] 会
            # 把 end_date 之后的数据也泄进运行窗（训练窗看见测试窗＝未来函数）。
            i1 = i0 + len(eval_dates)
            data_dates = all_dates[max(0, i0 - warm):i1]
        keep = np.isin(panel.dates, data_dates)
        meta = {k: v for k, v in panel.metadata.items() if k not in ("_active", "_slice_cache")}
        sub = BarPanel(data_dates, panel.symbols, {k: v[keep] for k, v in panel.arrays.items()}, meta)
        cache[key] = (sub, data_dates, eval_dates)
        # 封顶防膨胀：walk-forward 每折窗口不同，折数多时每折各挂一份字段矩阵拷贝
        # （挂在共享 panel.metadata 上，panel 存活即不释放）。32 份远超网格变体的
        # key 数；超限淘汰最早写入的窗口（WF 折序单调，不会再回访）。
        while len(cache) > 32:
            cache.pop(next(iter(cache)))
        return sub, data_dates, eval_dates

    def _ctx(self, date, bars) -> Context:
        self.current_date = date
        self.current_bars = bars
        return Context(self, date, bars)

    def _update_last_prices(self, bars) -> None:
        fill = getattr(bars, "fill_last_prices", None)
        if fill is not None:
            fill(self._last_prices)
            return
        for symbol, bar in bars.items():
            if bar.is_valid:
                self._last_prices[symbol] = bar.close

    def prev_date(self, date) -> Optional[pd.Timestamp]:
        i = int(self.dates.searchsorted(pd.Timestamp(date)))
        return self.dates[i - 1] if i > 0 else None

    def last_price(self, symbol: str) -> Optional[float]:
        return self._last_prices.get(symbol)

    def date_index(self, date) -> int:
        return int(self.dates.searchsorted(pd.Timestamp(date)))

    # ------------------------------------------------------------- 下单流程
    def _ref_price(self, symbol: str) -> float:
        """下单/投影共用的参考价：当日收盘有效用收盘，否则退最近价（无则 0）。"""
        bar = self.current_bars.get(symbol)
        return (bar.close if bar is not None and bar.is_valid else None) or self.last_price(symbol) or 0.0

    def _invalidate_projection(self) -> None:
        """投影缓存失效：新 bar 开始、或成交改变持仓后调用（现金实时读，不缓存）。"""
        self._proj = None
        self._holdings_total = None
        self._holdings_parts = {}

    def _pending_projection(self) -> tuple:
        """同日待撮合挂单的资金投影（挂单预留层）。

        旧行为：风控永远看 `account.cash`，同一 on_bar 里多笔满仓买单全部通过，
        到次日成交阶段才在 can_apply 被拒——失败静默且结果依赖提交顺序。
        现投影：
          * reserved：pending 买单按「参考价×(1+费率)×缓冲」占用的现金；
          * inflow：pending 卖单预期回款，按同一缓冲打折扣（覆盖隔夜跳空，
            保守口径；close 模式下同一收盘价估回款则基本无差）；
          * sell_qty：各标的已挂待卖量，用于净持仓/净仓位额度。
        投影只影响事前审批的缩量；真实成交仍由 account.can_apply 兜底。

        批次优化：同一根 bar 内第一次调用做全量扫描并缓存，此后由 submit_order
        在挂单入队时**增量维护**（rebalance_to 一次提交 2N 单不再 O(N²) 重扫）；
        每根 bar 开始与成交发生后缓存失效重扫。增量口径与全量扫描逐项一致。
        """
        if self._proj is not None:
            return self._proj[0], self._proj[1], self._proj[2]
        reserved = 0.0
        inflow = 0.0
        sell_qty: Dict[str, int] = {}
        buffer = max(1.0, float(self.config.cash_demand_pct))
        for o in self.pending:
            px = self._ref_price(o.symbol)
            if not px:
                continue
            rate = self.cost_model.estimated_cost_pct(o.symbol, o.side)
            if o.side is Side.BUY:
                reserved += o.quantity * px * (1 + rate) * buffer
            else:
                inflow += o.quantity * px * (1 - rate) / buffer
                sell_qty[o.symbol] = sell_qty.get(o.symbol, 0) + int(o.quantity)
        self._proj = [reserved, inflow, sell_qty]
        return reserved, inflow, sell_qty

    def _holdings_projection(self, pending_sells: Dict[str, int]) -> float:
        """持仓市值投影（扣同日待卖量）。首批 O(S) 扫描后缓存；此后每笔新挂卖单
        只重算该标的的贡献项（见 _proj_note_submit），大批次调仓不再每单全扫。"""
        if self._holdings_total is not None:
            return self._holdings_total
        total = 0.0
        for s, p in self.account.positions.items():
            part = max(0, p.quantity - pending_sells.get(s, 0)) * (self._last_prices.get(s) or p.avg_cost)
            self._holdings_parts[s] = part
            total += part
        self._holdings_total = total
        return total

    def _proj_note_submit(self, order: Order, price: float) -> None:
        """挂单入队后增量维护投影缓存（与全量重扫严格同口径，见 _pending_projection）。"""
        if self._proj is None or self._holdings_total is None or not price:
            return
        rate = self.cost_model.estimated_cost_pct(order.symbol, order.side)
        buffer = max(1.0, float(self.config.cash_demand_pct))
        if order.side is Side.BUY:
            self._proj[0] += order.quantity * price * (1 + rate) * buffer
        else:
            self._proj[1] += order.quantity * price * (1 - rate) / buffer
            sell_qty = self._proj[2]
            sell_qty[order.symbol] = sell_qty.get(order.symbol, 0) + int(order.quantity)
            # 持仓投影：该标的净量下降只影响它自己的贡献项
            p = self.account.positions.get(order.symbol)
            if p is not None:
                new_part = max(0, p.quantity - sell_qty[order.symbol]) * (self._last_prices.get(order.symbol) or p.avg_cost)
                self._holdings_total += new_part - self._holdings_parts.get(order.symbol, 0.0)
                self._holdings_parts[order.symbol] = new_part

    def submit_order(self, order: Order) -> Order:
        self._order_seq += 1
        order.id = self._order_seq
        order.created_date = self.current_date
        bar = self.current_bars.get(order.symbol)
        pos = self.account.position(order.symbol)
        price = self._ref_price(order.symbol)
        reserved, inflow, pending_sells = self._pending_projection()
        # 投影后的可用现金/净持仓/净总仓位：同日先卖后买的组合调仓不再被旧现金额卡死，
        # 多标的满仓买单也不会全部通过后再静默拒单。
        cash_proj = self.account.cash - reserved + inflow
        holdings_proj = self._holdings_projection(pending_sells)
        pos_net_qty = max(0, pos.quantity - pending_sells.get(order.symbol, 0))
        equity_proj = max(0.0, cash_proj) + holdings_proj
        verdict = self.risk.approve(
            order,
            cash=max(0.0, cash_proj),
            cash_buffer_pct=self.config.cash_demand_pct,
            position_quantity=pos_net_qty,
            position_available=pos.available,
            equity=equity_proj,
            position_value=pos_net_qty * price,
            total_holdings_value=holdings_proj,
            bar=bar,
            halted=self._halted,
            buy_forbidden=self._buy_forbidden,
        )
        if not verdict.approved:
            order.status = OrderStatus.REJECTED
            order.reject_reason = verdict.reason
            self._record_order(order)
            return order
        order.quantity = int(verdict.quantity)
        order.quantity = order.quantity - order.quantity % self.contract.lot_size if order.quantity >= self.contract.lot_size else order.quantity
        if order.quantity <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "取整后数量为 0（最小交易单位 100 股）"
            self._record_order(order)
            return order
        order.status = OrderStatus.PENDING
        self._record_order(order)
        self.pending.append(order)
        self._proj_note_submit(order, price)
        return order

    def _record_order(self, order: Order) -> None:
        """订单明细登记：record_orders=False（批量网格/大回测）时跳过，省内存与 DataFrame 构造。"""
        if self.config.record_orders:
            self.orders.append(order)

    def record_rejected_order(self, symbol: str, side: Side, quantity: int, reason: str) -> Order:
        self._order_seq += 1
        order = Order(symbol=symbol, side=side, quantity=quantity, reason=reason)
        order.id = self._order_seq
        order.created_date = self.current_date
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._record_order(order)
        return order

    def _process_pending(self, bars: Dict[str, Bar], date) -> None:
        if not self.pending:
            return
        valid = max(1, int(self.config.order_valid_bars))
        result = self.matcher.match(self.pending, bars, date)
        fills_by_id = {f.order_id: f for f in result.fills}
        keep: List[Order] = []
        for order, status, filled_qty, message in result.updates:
            order.status = status
            order.filled_quantity = filled_qty
            order.reject_reason = "" if status in (OrderStatus.FILLED, OrderStatus.PARTIAL) else message
            fill = fills_by_id.get(order.id)
            if fill is not None:
                blocked = self.account.can_apply(fill)
                if blocked:
                    order.status = OrderStatus.REJECTED
                    order.filled_quantity = 0
                    order.reject_reason = blocked
                    fill = None
                else:
                    self.account.apply_fill(fill)
            # 挂单生命周期（order_valid_bars，默认 1 = 现行「一次撮合机会后作废」口径）：
            # 停牌/无行情、限价未触及而 cancelled 的订单在有效期内跨 bar 续挂；
            # rejected（风控/封板/无可成交量）不续；partial 剩余部分仍当日作废（不变）。
            if status is OrderStatus.CANCELLED and valid > 1:
                attempts = self._order_attempts.get(order.id, 1)
                if attempts < valid:
                    self._order_attempts[order.id] = attempts + 1
                    order.status = OrderStatus.PENDING
                    order.reject_reason = ""
                    keep.append(order)
        self.pending = keep
        # 成交改变现金与持仓：下一批次（若有）的投影缓存必须重建
        self._invalidate_projection()

    # ------------------------------------------------------------- 调仓工具
    def rebalance_to(self, ctx: Context, targets: Mapping[str, float], reason: str = "") -> List[Order]:
        equity = ctx.equity
        if equity <= 0:
            return []
        held = set(ctx.positions.keys())
        wanted = {s: float(w) for s, w in targets.items() if w > 0}
        orders: List[Order] = []
        # 先卖后买，释放资金
        for symbol in sorted(held - set(wanted), key=lambda s: -ctx.holding(s)):
            qty = ctx.holding(symbol)
            orders.append(self.submit_order(Order(symbol=symbol, side=Side.SELL, quantity=qty, reason=f"{reason}:清仓")))
        for symbol, weight in wanted.items():
            px = ctx.price(symbol) or self.last_price(symbol)
            if not px:
                continue
            target_qty = int(math.floor(equity * weight / px / self.contract.lot_size) * self.contract.lot_size)
            current = ctx.holding(symbol)
            diff = target_qty - current
            if diff > 0:
                orders.append(self.submit_order(Order(symbol=symbol, side=Side.BUY, quantity=diff, reason=f"{reason}:加仓")))
            elif diff < 0:
                sellable = min(-diff, ctx.available(symbol))
                if sellable >= self.contract.lot_size or (-diff) == current:
                    orders.append(self.submit_order(Order(symbol=symbol, side=Side.SELL, quantity=sellable, reason=f"{reason}:减仓")))
        return orders

    # ------------------------------------------------------------- 收尾
    def _force_liquidate(self, date, panel: BarPanel) -> bool:
        """期末强制平仓：复用撮合层的卖出语义（滑点、参与率、跌停封板检查）。

        与正常撮合的唯一差别是价格锚点固定为最后收盘（_close_matcher，execution=close）：
        期末已无「次日开盘」。跌停封板/停牌/无行情的标的**保留持仓按最后价估值**，
        与正常撮合口径一致——不再无条件按原价硬平、不把滑点记成 0。
        """
        if not self.account.positions:
            return False
        any_sold = False
        bars = panel.bars_view(self.date_index(date))
        for symbol, pos in list(self.account.positions.items()):
            if pos.quantity <= 0 or pos.available <= 0:
                continue  # 无持仓或 T+1 当日买入份额本就不可卖
            bar = bars.get(symbol)
            if bar is None or not bar.is_valid:
                continue  # 停牌/无行情：保留持仓，按最后价估值
            order = Order(symbol=symbol, side=Side.SELL, quantity=pos.available,
                          reason="期末强制平仓（便于业绩归因）")
            fill, status, _message = self._close_matcher.match_one(order, bar, date)
            if fill is None or status is OrderStatus.REJECTED:
                continue  # 跌停封板等：卖不出去，保留持仓
            if self.account.can_apply(fill):
                continue
            self.account.apply_fill(fill)
            any_sold = True
        return any_sold

    # ------------------------------------------------------------- 导出
    def _fill_row(self, f: Fill) -> dict:
        return {
            "date": f.date,
            "order_id": f.order_id,
            "symbol": f.symbol,
            "side": f.side.value,
            "quantity": f.quantity,
            "price": f.price,
            "raw_price": f.raw_price,
            "gross_amount": f.gross_amount,
            "commission": f.commission,
            "stamp_tax": f.stamp_tax,
            "transfer_fee": f.transfer_fee,
            "other_fee": f.other_fee,
            "slippage_cost": f.slippage_cost,
            "total_cost": f.total_cost,
            "cash_flow": f.cash_flow,
            "reason": f.reason,
        }

    def _order_row(self, o: Order) -> dict:
        return {
            "id": o.id,
            "created_date": o.created_date,
            "symbol": o.symbol,
            "side": o.side.value,
            "order_type": o.order_type.value,
            "quantity": o.quantity,
            "limit_price": o.limit_price,
            "status": o.status.value,
            "filled_quantity": o.filled_quantity,
            "reject_reason": o.reject_reason,
            "reason": o.reason,
        }

    def _trade_row(self, t: Trade) -> dict:
        return {
            "symbol": t.symbol,
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "gross_pnl": t.gross_pnl,
            "cost": t.cost,
            "net_pnl": t.net_pnl,
            "return_pct": t.return_pct,
            "holding_days": t.holding_days,
            "holding_bars": t.holding_bars,
            "entry_order_id": t.entry_order_id,
            "exit_order_id": t.exit_order_id,
        }

    # ---------------------------------------------------------------- 权益
    def equity(self, date) -> float:
        return self.account.equity(self._last_prices)


def run_backtest(
    strategy: Strategy,
    data: Union[BarPanel, pd.DataFrame, Mapping[str, pd.DataFrame]],
    config: Optional[BacktestConfig] = None,
    lite: bool = False,
    **kwargs,
) -> BacktestResult:
    """便捷入口：`run_backtest(MyStrategy(), panel, BacktestConfig(...))`。

    lite=True 只产净值/指标（网格/WF 批量回测用），不构持仓/权重/明细表。
    """
    if config is None:
        config = BacktestConfig(**kwargs) if kwargs else BacktestConfig()
    elif kwargs:
        for key, value in kwargs.items():
            setattr(config, key, value)
    engine = BacktestEngine(config)
    return engine.run(strategy, data, lite=lite)
