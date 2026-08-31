"""研究 CLI：参数网格 & Walk-Forward。

示例：
  # 双均线网格（真实数据）
  python -m quant_sim.tools.research grid --strategy dual_ma --symbol 510300 \
      --data data/cn_a/daily --start 2022-01-01 --end 2026-08-28 \
      --grid fast=5,10,15,20,25 --grid slow=40,60,90,120

  # ETF 动量轮动 Walk-Forward（2 年训练 + 半年测试滚动）
  python -m quant_sim.tools.research wf --strategy momentum \
      --universe 510300 510500 159915 512880 588000 \
      --data data/cn_a/daily --train 488 --test 122 \
      --grid lookback=40,60,120 --grid top_n=1,2
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from quant_sim.research import (  # noqa: F401
    grid_search,
    make_backtest_config,
    walk_forward,
)
from quant_sim.strategies import DualMAStrategy, MomentumRankingStrategy


def parse_grid(items):
    grid = {}
    for item in items or []:
        key, _, values = item.partition("=")
        vals = []
        for raw in values.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                vals.append(int(raw))
            except ValueError:
                try:
                    vals.append(float(raw))
                except ValueError:
                    vals.append(raw)
        grid[key.strip()] = vals
    return grid


def build(strategy_name, args, panel):
    if strategy_name == "dual_ma":
        sym = args.symbol or panel.symbols[0]

        def factory(**kw):
            return DualMAStrategy(symbol=sym, **kw)

        return factory
    if strategy_name == "momentum":
        universe = args.universe or panel.symbols

        def factory(**kw):
            return MomentumRankingStrategy(universe=universe, **kw)

        return factory
    raise SystemExit(f"未注册策略: {strategy_name}（支持 dual_ma / momentum）")


def load(args):
    if args.data and os.path.isdir(args.data):
        from quant_sim.data.loader import load_panel

        symbols = list(args.universe or [])
        if getattr(args, "benchmark", None) and args.benchmark not in symbols:
            symbols.append(args.benchmark)  # 基准必须在面板里
        return load_panel(args.data, symbols=symbols or None)
    from quant_sim.data.sample import make_demo_panel

    print("⚠️ 未提供 --data，使用合成演示数据")
    return make_demo_panel()


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--strategy", required=True, choices=["dual_ma", "momentum"])
    common.add_argument("--data", default="data/cn_a/daily")
    common.add_argument("--symbol", default=None, help="dual_ma 标的代码")
    common.add_argument("--universe", nargs="*", default=[], help="momentum 股票池；也是 --data 目录的过滤")
    common.add_argument("--start", default=None)
    common.add_argument("--end", default=None)
    common.add_argument("--cash", type=float, default=1_000_000)
    common.add_argument("--benchmark", default=None)
    common.add_argument("--execution", default="next_open", choices=["next_open", "close"])
    common.add_argument("--commission-wp", dest="commission_wp", type=float, default=2.5, help="佣金（万分之）")
    common.add_argument("--min-comm", dest="min_comm", type=float, default=5.0)
    common.add_argument("--stamp-wp", dest="stamp_wp", type=float, default=5.0, help="印花税卖出（万分之）")
    common.add_argument("--slip-model", dest="slip_model", default="spread", choices=["spread", "percent", "tick", "none"])
    common.add_argument("--slip-value", dest="slip_value", type=float, default=5e-4)
    common.add_argument("--participation", type=float, default=0.05)
    common.add_argument("--max-dd", dest="max_dd", type=float, default=0.0)
    common.add_argument("--no-block-limit", dest="no_block_limit", action="store_true")
    common.add_argument("--rank", default="夏普比率")
    common.add_argument("--grid", action="append", help="形如 fast=5,10,20，可多次")
    common.add_argument("--out", default="results/research")

    parser = argparse.ArgumentParser(description="参数研究工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("grid", parents=[common], help="全区间网格搜索")
    wf = sub.add_parser("wf", parents=[common], help="Walk-Forward 滚动样本外")
    wf.add_argument("--train", type=int, default=488, help="训练窗交易日数")
    wf.add_argument("--test", type=int, default=122, help="测试窗交易日数")
    args = parser.parse_args()

    panel = load(args)
    # 与网页侧栏同一配置构造器：成本/滑点/参与率/熔断不再是 CLI 缺省与 UI 实配的兩套口径
    cfg = make_backtest_config(
        start=args.start,
        end=args.end,
        cash=args.cash,
        execution=args.execution,
        commission=args.commission_wp / 10000,
        min_comm=args.min_comm,
        stamp=args.stamp_wp / 10000,
        slip_model=args.slip_model,
        slip_value=args.slip_value,
        participation=args.participation,
        max_dd_halt=args.max_dd,
        benchmark=args.benchmark,
        block_limit=not args.no_block_limit,
    )
    grid = parse_grid(args.grid) or {"fast": [10, 20], "slow": [60, 120]}
    factory = build(args.strategy, args, panel)
    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.strategy}_{'_'.join(sorted(panel.symbols)[:3])}"

    if args.cmd == "grid":
        df = grid_search(factory, grid, panel, cfg, rank_by=args.rank, max_trades_filter=3)
        cols = [c for c in df.columns if c != "_error"]
        print(df[cols].head(30).to_string(index=False))
        path = os.path.join(args.out, f"grid_{tag}.csv")
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n完整网格 → {path}（{len(df)} 组合）")
        print("提示：只看第 1 名容易过拟合；检查前 5 名参数是否聚成一片（邻域稳定），并跑 wf 验证。")
    else:
        res = walk_forward(factory, grid, panel, cfg, train_days=args.train, test_days=args.test, rank_by=args.rank)
        print(res.summary())
        res.folds.to_csv(os.path.join(args.out, f"wf_folds_{tag}.csv"), index=False, encoding="utf-8-sig")
        res.oos_equity.rename("oos_equity").to_csv(os.path.join(args.out, f"wf_oos_{tag}.csv"), encoding="utf-8-sig")
        print(f"\n折表与 OOS 净值已写入 {args.out}/")


if __name__ == "__main__":
    main()
