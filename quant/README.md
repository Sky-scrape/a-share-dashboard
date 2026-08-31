# 量化引擎（内置于 A股看板）

A 股 / 场内 ETF 日线**规则诚实**回测引擎。2026-08-30 由独立项目「量化平台」迁入本项目
`quant/`，Streamlit 界面已移除——日常操作全部在原生工作台 **http://127.0.0.1:8000/quant**
（造策略 / 回测报告 / 策略库 / 选股台 / 今日信号 / 参数研究，由 server 的
`/api/quant/*` 直调本引擎），本目录只保留纯引擎 + CLI + 测试。

完整设计见 **[docs/design.md](docs/design.md)**，演进记录见 **[docs/changelog.md](docs/changelog.md)**。

## 引擎纪律（全部建模）

T+1（按日历日解锁）、涨跌停禁成交、整手（100 股）、佣金/印花税/过户费、价差滑点、
参与率上限、停牌跳过、除权参考价口径校验（manifest 身份证）、防未来函数
（`ctx.history` 默认截至昨收，信号 T 日产生、T+1 开盘撮合）。

## 直接写策略（Python）

```python
from quant_sim import BacktestConfig, run_backtest
from quant_sim.data.loader import load_panel
from quant_sim.core.strategy_base import Strategy

class BuyEveryDip(Strategy):
    def on_bar(self, ctx):
        if ctx.pending:
            return
        df = ctx.history("510300", 20)          # 默认截至昨日，防未来函数
        if df is not None and len(df) == 20 and ctx.holding("510300") == 0:
            if df["close"].iloc[-1] < df["close"].mean() * 0.95:
                ctx.target_percent("510300", 0.5)

panel = load_panel("data/cn_a/daily", symbols=["510300"])
r = run_backtest(BuyEveryDip(), panel, BacktestConfig(initial_cash=1e6, benchmark="000300"))
print(r.summary())
r.save("results", "my_strategy")                # CSV + JSON + 自包含 HTML 报告
```

## CLI（无人值守 / 定时任务可用）

```bash
# 数据：个股走 hithink 本地 DuckDB（秒级），ETF 走远端 fund.history
python -m quant_sim.data.hithink export --symbols 510300 159915 --start 2021-09-01
python -m quant_sim.tools.demo                  # 合成数据冒烟
python -m quant_sim.tools.research grid --strategy dual_ma --symbol 510300 \
  --universe 510300 --benchmark 000300 --grid fast=5,10,15,20 --grid slow=40,60,90
python -m quant_sim.tools.research wf --strategy momentum --universe 510300 510500 159915 \
  --benchmark 000300 --train 366 --test 122 --grid lookback=40,60,120
python -m quant_sim.tools.signals run --name 存档名      # 明日开盘执行清单
python -m quant_sim.tools.ledger add|list|undo --name X   # 成交轻台账
python -m quant_sim.research.screener --template 趋势多头 # 条件选股
python -m pytest tests/ -q                      # 引擎回归（110+ 项）
```

所有落盘路径经 `quant_sim/paths.py` 解析到本目录（换 cwd 启动不丢存档）。
策略存档在 `strategies_store/*.json`（规则/代码/内置三类，未知 spec 键报错不静默；
⚠️ 代码类存档默认拒绝回放，需 `--yes-run-code` 或原生页显式勾选）。

## 目录

```text
quant_sim/paths     项目根与落盘目录单一来源
quant_sim/core      引擎（事件循环/撮合/账户/风控/规则/成本）
quant_sim/data      数据加载（manifest 口径校验）、复权、hithink 桥接、合成样例
quant_sim/strategies 内置策略（双均线/动量轮动/均值回归/声明式规则/沙箱代码/存档）
quant_sim/metrics   绩效指标（244 交易日年化口径）+ 归因
quant_sim/report    CSV/JSON/自包含 HTML 导出
quant_sim/research  runner/网格/WF/多策略对比/因子/情绪/选股/敏感性
quant_sim/tools     研究/信号/台账/拉数/演示 CLI
tests/              引擎回归（不含任何 UI 测试——UI 断言在项目根 tests/smoke.py）
```
