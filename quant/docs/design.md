# 量化模拟平台设计方案（A 股 / 场内 ETF · 日线）

> 版本 0.2 ｜ 2026-08-30 ｜ 状态：M1+ 已实现；按日期的演进记录已拆到 **[docs/changelog.md](changelog.md)**
>
> 本文档只维护**架构、规则口径、数据契约与路线图**；测试项数以
> `python -m pytest tests/ --collect-only -q | tail -1` 为准（不在文档里写死）。

---

## 1. 目标与非目标

**目标**

- 让我能用 Python 写策略，在 A 股历史日线上自动模拟交易，得到**可信**的
  交易记录、净值曲线、风险指标与绩效报告。
- 交易规则（T+1、涨跌停、整手、费用、停牌）必须真实建模——**回测可信度
  来自规则的诚实，而不是收益的漂亮**。
- 架构上为后续三阶段留好接口：参数优化/滚动回测 → 实时模拟盘 → 实盘接口。

**非目标（当前阶段）**

- 不做多市场（期货/加密/外汇）；
- 不做分钟线/tick 撮合（2026-08-30 起：面板已按频率解耦，日内数据会被引擎**硬拒**而非
  静默失真；真正接入需要 ExecutionModel，见 §5.3 与 changelog）；
- 不做多用户、Web 账号体系（Streamlit 单页自用）。

---

## 2. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│  界面层   Streamlit 单页（MVP） → React+ECharts（后期）│
├─────────────────────────────────────────────────────┤
│  任务层   CLI / run_backtest（MVP）→ FastAPI+Celery   │
├─────────────────────────────────────────────────────┤
│  引擎层   BacktestEngine（事件驱动、逐日推进）          │
│    ├── BarPanel      行情容器（列式 numpy）             │
│    ├── Strategy      策略接口（Context 沙盒）           │
│    ├── RiskManager   事前审批 + 事中熔断                │
│    ├── MatchingEngine撮合（纯函数，回测/模拟盘共用）     │
│    ├── Account       现金/T+1 批次/FIFO 盈亏            │
│    └── Metrics       绩效指标（统一口径）               │
├─────────────────────────────────────────────────────┤
│  规则层   Contract（涨跌停/整手/tick/T+1）+ CostModel    │
├─────────────────────────────────────────────────────┤
│  数据层   akshare 拉取 / 本地 CSV·Parquet / 合成数据      │
│           DuckDB + Parquet（后期）→ PostgreSQL（结果库）  │
└─────────────────────────────────────────────────────┘
```

核心链路只有一条：

> **数据 → 信号（策略）→ 风控 → 订单 → 撮合 → 成交 → 账户 → 净值 → 报告**

模拟盘阶段复用同一链路，只把「历史 BarPanel 迭代器」换成「实时行情推送」，
把「回看撮合」换成「盘口前看撮合」——这就是 `MatchingEngine` 被写成纯函数的原因。

---

## 3. A 股交易规则建模（本平台的灵魂）

| 规则 | 建模方式 | 代码位置 |
|---|---|---|
| T+1 | 每笔买入生成 `Lot(available=0)`，次日开盘统一解锁；卖出只扣可卖批次 | `core/account.py` |
| 涨跌停 | 以 `pre_close`（除权参考价口径）计算 ±10%/±20%/±5%/±30%，round 到 0.01；一字封板拒单 | `core/contract.py`、`core/matching.py` |
| 最小交易单位 | 100 股整手向下取整；清仓允许卖出零股 | `Contract.round_quantity` |
| 板块差异 | 主板 10% / 创业板·科创板 20% / ST 5% / 北交所 30%；ETF 默认 10%，跟踪双创指数的用 `limit_overrides` 配 20% | `Contract.limit_pct` |
| 佣金 | 万 2.5 双边、单笔最低 5 元；ETF 无最低佣金（可配） | `core/cost.py` |
| 印花税 | 卖出单边千 0.5（2023-08 税率）；**ETF 免** | `core/cost.py` |
| 过户费 | 沪深双边 0.001%；ETF 免 | `core/cost.py` |
| 滑点 | `spread`（当日振幅×0.5 与 万5 取小，默认）/ `percent` / `tick` / `none` | `CostModel.slippage` |
| 流动性 | 单边成交量 ≤ 当日量 × 参与率（默认 5%），超出部分成交、剩余作废 | `core/matching.py` |
| 停牌 | 当日无有效 bar → 订单作废；净值按最近有效价/成本估值 | `BarPanel.active`、撮合层 |
| 除权除息 | 数据层提供正确的 `pre_close`（除权参考价），价格序列保证复权一致 | `data/`，见 §6 |
| 资金 | 不允许透支；买入按「收盘价×(1+费率)×cash_demand_pct」预估缩量；**同日挂单预留**：买单占用从投影现金扣除、卖单折价回款计入额度，组合调仓与提交顺序无关 | `RiskManager.approve`、`engine.submit_order` 投影层 |
| 挂单生命周期 | 默认 `order_valid_bars=1`（一次撮合机会后作废）；>1 时停牌/限价未触达的 cancelled 挂单跨 bar 续挂；rejected 与 partial 剩余不续 | `engine._process_pending` |

**撮合时序（默认 `execution="next_open"`）**：

```text
T 日 on_bar：策略只能看到「截至 T-1 的成交」+「截至 T 收盘的行情」→ 提交委托
T+1 日：解锁 T 日买入份额 → 以 T+1 开盘价撮合（含滑点、涨跌停、参与率检查）→ …
```

这是日线策略最接近实盘的假设：**收盘后看到信号、次日开盘才能成交**。
`execution="close"`（当日收盘成交）用于乐观情形对比，报告中必须注明。

> ⚠️ 已知简化（P1 待做）：开盘集合竞价成交价偏差、日内触及止盈止损价的
> 「bar 内路径」假设（当前止盈止损只在 on_bar 时点判断）、分红现金再投资、
> 限价/止损单类型（OrderType 枚举已就位但撮合只按市价价锚；跨日挂单能力
> `order_valid_bars` 已实装，等待单类型接入）。T+1 解锁语义已改为按日历日
> 变更触发（日内面板会被引擎硬拦，不会静默变 T+0）。

---

## 4. 策略 API 设计

### 4.1 事件驱动（正式接口）

```python
from quant_sim import BacktestConfig, run_backtest
from quant_sim.core.strategy_base import Strategy

class MyStrategy(Strategy):
    def on_start(self, ctx):        # 一次
        ...
    def on_bar(self, ctx):          # 每个交易日
        df = ctx.history("510300", 60)     # 截至【昨日】的 60 根 bar（防未来函数）
        if 金叉(df) and not ctx.pending:
            ctx.target_percent("510300", 0.5)   # 目标权益占比，自动整手/资金检查
        if 死叉(df) and ctx.holding("510300"):
            ctx.sell("510300", quantity=ctx.holding("510300"))
    def on_finish(self, ctx):       # 结束前
        ...

result = run_backtest(MyStrategy(), panel, BacktestConfig(initial_cash=1e6))
print(result.summary())
```

`Context` 是策略的沙盒：只能查询**当前信息集**（现金、持仓、可卖量、
当日 bar、截至昨日的 history）。`ctx.history()` 默认不含当日收盘——
这是引擎层面强制的防未来函数护栏。

### 4.2 信号式（快速研究）

`CrossSectionalStrategy.select(ctx) -> {标的: 目标权重}`，引擎负责调仓、
整手、风控。内置示例：横截面动量轮动。

### 4.3 内置策略库（`quant_sim/strategies/`）

| 策略 | 范式 | 说明 |
|---|---|---|
| `DualMAStrategy` | 单标的趋势 | 均线金叉/死叉 + 可选 ATR 移动止损 |
| `MomentumRankingStrategy` | 横截面组合 | 月度 Top-N 动量、绝对动量空仓过滤 |
| `MeanReversionStrategy` | 单标的回归 | 布林带分批抄底、中轨离场 |
| `GenericRuleStrategy`（`rule_based.py`） | 声明式规则 | 网页表单拼的「指标+条件+风控」JSON 直接执行；`generate_python_code()` 导出等价裸代码，两路径逐分钱一致（测试锁定） |
| 沙箱代码策略（`sandbox.py`） | 在线写码 | 受限 builtins + import 白名单 + **AST 逃逸链黑名单** exec 用户代码；无超时（卡死需重启，实话已写入注释）；kind=code 存档回放需显式 allow_exec 确认 |

### 4.31 界面结构（`web/`）

策略台主干（`streamlit_app.py`：侧栏数据管线 + 造策略/策略库/今日信号）+ 研究与报告
四子页（`page_labrep.py`，显式 ctx 传参）+ 选股台板块（`page_screener.py`）。
spec 语义（验证/回填/导出）全部在 `rule_based`/`store`，UI 只做 widget 搬运；
tab 体内禁用 `st.stop()`（AST 测试锁）。历史功能清单见 changelog。

---

### 4.4 研究闭环（M1，`quant_sim/research/`）

* **统一 runner（`research/runner.py`，8/30）**：`run_variants(variants, build, panel, cfg,
  metrics, config_fn, lite)` 是网格/WF 内层/多策略对比/参数邻域/成本压测共用的唯一批跑
  实现（错误行不中断、列构造不四处复写）；`make_backtest_config` 是 UI 侧栏与 CLI 共用的
  唯一配置构造点；`LOWER_IS_BETTER`/`GRID_METRICS` 单一来源；`stress_config` 成本压测。
* `grid_search(factory, grid, panel, cfg, rank_by)`：笛卡尔积回测→按指标排序的
  参数平面（内部委托 run_variants，引擎开 lite 模式）；错误参数不中断网格（`_error` 行）；
  交易数过少的组合自动沉底。用法纪律：**邻域成片才是信号，孤立尖峰都是过拟合**。
* `walk_forward(..., train_days, test_days, warmup_days, decay_warn)`：滚动「训练窗选参 →
  紧邻测试窗验证」；**每折数据窗前推 warmup（缺省按网格最大数值参数自动估），策略在预热段
  真实建仓，长回看不被饿死、折首段不再被拍平**；测试窗重叠（step<test）直接拒绝；
  输出结构化 folds 表（含每折衰减率）、相邻折参数迁移、`verdict` 结构化裁决与
  OOS 拼接净值（首日相对预热末权益，非初始资金）。常驻性质测试锁定：
  **评估窗之后的数据怎么变，窗内 OOS 结果必须不变**。
* **因子工作台口径**：`run_factor_study(..., entry="t1_open")`——未来收益默认
  T+1 开盘建仓→T+1+k 开盘离场，与引擎 next_open 纪律闭环；t_close 旧口径仅作对照
  （含不可交易隔夜段，分层净值/IC 虚高）。**8/30 前的因子分层结论需重跑**。
* CLI：`python -m quant_sim.tools.research grid|wf ...`（支持与 UI 同源的全部摩擦参数），
  结果写 `results/research/`。

## 5. 数据层

### 5.1 数据契约（必须遵守）

每标的一个文件（Parquet/CSV），列：`open high low close volume amount pre_close`，
索引/列含日期。**接受任意中文列名**（东财/通达信导出直接可用，自动映射）。

- **复权口径随数据落盘（8/30）**：每个导出目录带 `_manifest.json`（每标的一个条目：
  adjust 口径 qfq/hfq/raw/dividend_reinvested/none、源视图、行数、日期范围、导出时间、
  错价数）。loader 读入并校验期望口径：个股口径（qfq/hfq/raw）不一致 →
  `panel.metadata["manifest_mismatch"]` → 页面红条阻断提示；手工文件无记录 → 黄条告知。
  涨跌停判定依赖 pre_close 口径，混入 raw 序列会整体错位且静默——必须在入口拦截。
  内部统一用复权价 + `pre_close`；长期方向仍是**原始价 + 累计复权因子**（qfq 历史会随
  新分红被改写，hfq 是不变量），manifest 让口径至少可追溯、可校验。
- **成交量**：股（akshare 的“手”已在 fetch 层 ×100）。
- **停牌**：volume=0 或当日整行缺失。
- **日历**：MVP 用「全部标的有行情日期的并集」；生产替换为交易所日历
  （`exchange_calendars` XSHG/XSHE）。

### 5.2 数据来源

| 来源 | 状态 | 用法 |
|---|---|---|
| **hithink-finance 本地库（A 股个股）** | ✅ 主数据源 | `python -m quant_sim.data.hithink export --symbols 600519 000001 --start 2020-01-01`；只读 DuckDB（v_daily_qfq/hfq/raw），覆盖 2016 至今 5551 只（含退市）；同步由 `hithink-finance data sync` 负责 |
| **hithink fund.history（场内 ETF 日线）** | ✅ 远端 | `export --symbols 510300 159915`（5/15 开头代码自动分流）；**滚动 5 年窗口**（超出钉到窗口内并告警）；**未复权市价**；已用本地库交易日历过滤非开市日脏数据（fund.history 会返回补班日假行，且 date_ms 为北京零点 epoch，需 +8h 换算） |
| **hithink index.history（指数基准）** | ✅ 远端 | `export --symbols 000300`（裸代码策展名单识别，或带后缀 000001.SH）；同为滚动 5 年窗口；仅做基准曲线（不可交易） |
| 合成演示数据 | ✅ 内置 | `make_demo_panel()`，含涨跌停/停牌/除权压力场景，仅验证引擎 |
| 本地 CSV/Parquet | ✅ 支持 | Streamlit 上传 / `load_panel("data/cn_a/daily")` |
| akshare | ⚠️ 备选 | `quant_sim/tools/fetch.py`（本机代理屏蔽东财接口，需换网络） |
| tushare / 券商 | 🔜 P1 | 同契约落 Parquet 即可接入 |

数据质量护栏：导出时审计「单日涨跌幅超出品种限制 + 0.6% 容差」的行并记录
（厂商偶发错价，如 159915 在 2024-09-30 收盘价偏低导致次日假 +24%）；审计结果进
manifest（错价天数不再只存活于进程全局字典）；引擎按 pre_close 上限自动保守拒单，
不篡改原始数据。板块/品种归属（场内基金前缀）由 `Contract.FUND_PREFIXES` 单一来源，
20% 品种由 `CN_20PCT_FUNDS` 策展名单处理，发现新品种直接补；本地库不提供 ST 标记
（is_st 恒 False，ST 股涨跌停按常规幅度处理，偏乐观，属已知边界）。

### 5.3 存储演进路线

```text
MVP:   data/cn_a/daily/*.parquet + BarPanel(numpy)     —— 日线×百标的，秒级
P1:    DuckDB 做 bars 表 + 交易日历表 + 复权因子表
P2:    结果入库 PostgreSQL（strategies/backtests/orders/fills/trades/equity_curves/metrics）
```

引擎主循环为「日期迭代器 + 当日截面」（BarsView 懒构造），迭代结构与 bar 频率解耦；
但撮合价格锚点仍是日线假设（{next_open, close}），**日内面板会被 engine.run 硬拒**，
接入分钟线的前置是 ExecutionModel 重构（日内路径撮合/会话解锁语义），而非「零改动」。
T+1 解锁已改为按日历日变更触发，不会再静默退化为 T+0。

### 5.4 结果库表（P1 落地，字段与 MVP 导出对象一一对应）

`strategies(id,name,code_path,config_json)` / `backtests(id,strategy_id,range,cash,status)`
/ `orders(id,backtest_id,symbol,side,qty,price,status,reason)` / `fills(order_id,date,price,qty,commission,stamp_tax,transfer_fee,slippage)`
/ `trades(配对后的开平仓)` / `equity_curves(date,cash,holdings,equity,drawdown)` / `metrics(backtest_id,指标kv)`

---

## 6. 绩效指标（统一口径，写死在 `metrics/performance.py`）

- 年化=复利，一年 **244** 个交易日；夏普/索提诺基于日超额收益；
- 最大回撤 + 最长水下天数；卡玛=年化/回撤；
- 胜率/盈亏比/平均持仓周期基于 **FIFO 配对的 Trade（费用已摊入）**；
- 换手率、总费用及分项、滑点成本、费用占初始资金比；
- 基准对照：超额收益、Beta、跟踪误差、信息比率、相关性、CAPM Alpha。

期末统一强制平仓（fills 中 reason 标记），保证不同策略同一口径可比；信号模式
`liquidate_on_end=False` 保留期末持仓+待执行挂单（明日执行清单的基石）。预热段
（warmup_bars）的成交/已实现交易不计入窗口指标（账户评估窗视图过滤）。
展示口径唯一实现：`report/formatter.display_value`（表格/卡片/摘要共用，
performance._display 与 exporter._card_value 均委托）。

---

## 7. 防过拟合与防失真清单（工程强制）

| 陷阱 | 平台对策 |
|---|---|
| 未来函数 | `ctx.history` 默认截至昨日；信号→次日开盘撮合；因子研究同样按 T+1 开盘建仓口径（entry=t1_open）；WF 常驻「窗后数据不改窗内结果」性质测试 |
| 成交过乐观 | 默认 spread 滑点 + 参与率 5% + 涨跌停拒单，全部可调可查；买入预估含 cash_demand_pct 缓冲 |
| 幸存者偏差 | 数据契约要求含退市标的；主数据源 hithink 库含已退市股（5551 只）；P1 做时点成分股池；选股台明标「当日截面快照不可回测」（前视） |
| 复权不可复现 | 契约强制 pre_close；落盘带 `_manifest.json` 口径身份证，加载时校验+页面阻断告警；长期方向 hfq 存储 |
| 费用漏算 | ETF 免印花税/过户费等规则内置于 CostModel，报告展示分项；压测翻倍成本盈转亏即红 |
| 过拟合 | 网格+WF（含 warmup、折重叠拒绝、参数迁移、结构化衰减裁决）已落地；参数邻域扰动孤立尖峰告警；纪律入口文案常驻 |
| 组合调仓次序效应 | 同日挂单资金预留/回款投影，权重目标可复现（不再依赖提交顺序） |
| 研究入口口径漂移 | UI/CLI 共用 make_backtest_config；批跑共用 run_variants；展示共用 display_value |
| 存档回放语义漂移/代码执行 | spec 未知键 raise（不静默吞）；kind=code 存档默认拒绝 exec，需显式确认；沙箱 AST 逃逸链黑名单 |

---

## 8. 目录结构（当前仓库）

```text
量化平台/
├── docs/design.md               # 本文档（架构/口径/契约/路线图）
├── docs/changelog.md            # 按日期的演进记录（从 design 拆出）
├── quant_sim/
│   ├── paths.py                 # 项目根与默认落盘目录单一来源
│   ├── core/                    # 引擎：types(含 BarsView)/config/contract/cost/matching/account/risk/engine
│   ├── data/                    # loader(含 manifest 校验) / manifest / hithink 桥接 / adjustment / sample
│   ├── strategies/              # 内置策略 + rule_based(spec 单一事实源) + sandbox(AST 闸) + store(存档)
│   ├── metrics/                 # 绩效指标/归因
│   ├── report/                  # formatter(显示唯一实现) + exporter + templates/report.html
│   ├── research/                # runner(统一批跑) / grid_walkforward / factor / compare / sensitivity / sentiment / screener
│   └── tools/                   # research/signals/ledger/fetch/demo CLI
├── web/
│   ├── streamlit_app.py         # 策略台主干：侧栏+造策略/策略库/今日信号（研究区已拆出）
│   ├── page_labrep.py           # 研究与报告四子页（参数研究/因子/情绪/报告），显式 ctx 传参
│   ├── page_screener.py         # 🎯 选股台板块
│   └── theme.py                 # 夜盘主题
├── strategies_store/            # 策略存档 JSON（版本/备注/元数据）
├── tests/                       # 规则/数据/研究/工坊/选股/设计调整回归锁
├── data/ · results/             # 行情(含 _manifest.json) / 回测输出
└── requirements.txt
```

---

## 9. 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M0（本文档+代码）** | 引擎、A 股规则、费用滑点、指标、Streamlit、报告、测试 | ✅ 完成 |
| **M1 研究闭环** | 网格/WF/真实数据/指数基准/策略工坊/存档/盘后信号/归因/对比；定时信号自动化已上线（17:30）；ST/退市股票池界面化仍待做 | ✅ 完成 |
| **M1.5 诚实性/地基加固（8/30）** | 交易日解耦+日内硬拦、挂单预留、warmup、因子 t1_open、manifest、spec 单一事实源、沙箱加闸、统一 runner、性能 ~6× | ✅ 完成 |
| **M2 工程化** | FastAPI + Celery/Redis 任务化、结果落库（数据侧 DuckDB 已由 hithink 桥接实现）、多账户、限价/止损单全类型（order_valid_bars 续挂已就位）、除权因子表 | |
| **M3 模拟盘（2-3 个月）** | 实时行情（WebSocket/东财推送）、当日撮合（前置：ExecutionModel + 日内面板解禁）、策略启停控制台、成交对账、告警 | |
| **M4 实盘预留** | 券商/柜台接口抽象（下单/回报/资金同步），模拟-实盘同一策略代码 | |

---

## 10. 快速开始

```bash
pip install -r requirements.txt

# 1) 跑演示（合成数据，秒级）
python -m quant_sim.tools.demo

# 2) 打开界面
streamlit run web/streamlit_app.py

# 3) 用自己的数据：把每标的一个 CSV/Parquet 放进 data/cn_a/daily/
#    （东财导出中文列名即可，引擎自动识别），CLI 加 --data data/cn_a/daily

# 4) 网络可用时拉真实数据
python -m quant_sim.tools.fetch --symbols 510300 510500 159915 --start 20190101 --end 20251231

# 5) 回归测试
python -m pytest tests/ -q
```

写自己的策略：继承 `Strategy`，参考 `quant_sim/strategies/moving_average.py`。
