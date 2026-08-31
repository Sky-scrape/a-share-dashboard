"""CLI 演示入口：

    python -m quant_sim.tools.demo                    # 合成数据 + 双均线 + 动量轮动
    python -m quant_sim.tools.demo --data data/cn_a/daily --symbol 510300
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quant_sim.core.config import BacktestConfig
from quant_sim.core.engine import run_backtest
from quant_sim.strategies import DualMAStrategy, MomentumRankingStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股日线回测 MVP 演示")
    parser.add_argument("--data", default=None, help="行情目录（每标的一个 parquet/CSV）；缺省用合成演示数据")
    parser.add_argument("--symbol", default="510300", help="双均线标的")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--cash", type=float, default=1_000_000)
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    if args.data and os.path.isdir(args.data):
        from quant_sim.data.loader import load_panel

        panel = load_panel(args.data)
        note = f"本地数据 {args.data}"
    else:
        from quant_sim.data.sample import make_demo_panel

        panel = make_demo_panel()
        note = "⚠️ 合成演示数据（仅验证引擎，不代表真实行情）"
        print(note)

    cfg = BacktestConfig(start_date=args.start, end_date=args.end, initial_cash=args.cash, benchmark="510300")

    print("\n########## 策略一：双均线（%s）##########" % args.symbol)
    r1 = run_backtest(DualMAStrategy(args.symbol, 20, 60), panel, cfg)
    print(r1.summary())
    paths1 = r1.save(args.out, name="dual_ma", formats=("csv", "json", "html"), data_note=note)

    print("\n########## 策略二：ETF 动量轮动（月度 Top2）##########")
    r2 = run_backtest(MomentumRankingStrategy(panel.symbols, lookback=60, top_n=2), panel, cfg)
    print(r2.summary())
    paths2 = r2.save(args.out, name="momentum", formats=("csv", "json", "html"), data_note=note)

    print("\n输出：")
    for p in paths1 + paths2:
        print("  ", p)


if __name__ == "__main__":
    main()
