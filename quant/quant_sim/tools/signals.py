"""每日盘后信号：对存档/构建的策略跑最新数据，输出「明日开盘执行清单」。

原理：回测窗口跑到最新交易日且 **期末不强制平仓**（config.liquidate_on_end=False）。
最后一天的待执行挂单（status=pending）就是明天开盘要成交的信号；持仓快照就是今晚的仓位。

CLI：
    python -m quant_sim.tools.signals list                      # 看有哪些存档
    python -m quant_sim.tools.signals run --name 我的双均线      # 跑信号并写 results/signals/<日期>_<名>.md
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from typing import Dict, List, Optional

import pandas as pd

from quant_sim import paths as _paths
from quant_sim.core.fsutil import save_text_atomic

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def compute_signals(strategy, panel, config) -> Dict[str, object]:
    """跑一遍回测（不平仓），返回信号 dict：date/equity/cash/positions/orders。"""
    from quant_sim.core.engine import run_backtest

    cfg = dataclasses.replace(config, liquidate_on_end=False)
    result = run_backtest(strategy, panel, cfg)
    last = result.dates[-1]

    pos_rows = []
    for s in result.symbols:
        qty = int(result.positions.loc[last, s]) if s in result.positions.columns else 0
        if qty > 0:
            pos_rows.append({"标的": s, "持仓股数": qty})
    positions = pd.DataFrame(pos_rows)

    orders = result.orders
    pending = orders[orders["status"] == "pending"].copy() if len(orders) else pd.DataFrame()
    rejected_today = orders[(orders["status"] == "rejected") & (orders["created_date"] == last)] if len(orders) else pd.DataFrame()

    return {
        "date": last,
        "equity": float(result.equity.iloc[-1]),
        "cash": float(result.cash.iloc[-1]),
        "positions": positions,
        "orders": pending,
        "rejected_today": rejected_today,
        "result": result,
    }


def signal_report(sig: Dict[str, object], name: str = "", note: str = "") -> str:
    """把信号 dict 渲染成 Markdown 日报文本。"""
    d = pd.Timestamp(sig["date"]).date()
    eq, cash = float(sig["equity"]), float(sig["cash"])
    lines = [
        f"# 盘后信号 · {name or '策略'} · {d}",
        "",
        f"- 总权益 **{eq:,.0f}** ｜ 现金 {cash:,.0f}（{cash / eq:.1%}）" if eq else "- 权益数据缺失",
    ]
    pos: pd.DataFrame = sig["positions"]
    if len(pos):
        lines.append(f"- 当前持仓 {len(pos)} 个标的")
    ord_: pd.DataFrame = sig["orders"]
    if len(ord_):
        lines.append(f"- **明日开盘待执行 {len(ord_)} 单**")
    else:
        lines.append("- 明日无调仓信号（维持现有仓位）")
    lines.append("")

    if len(ord_):
        lines.append("## 明日执行清单")
        lines.append("")
        lines.append("| 方向 | 标的 | 股数 | 参考现价 | 原因 |")
        lines.append("|---|---|---|---|---|")
        res = sig["result"]
        last = res.dates[-1]
        for _, o in ord_.iterrows():
            px = None
            try:
                px = float(res.panel.bar(o["symbol"], last).close) if res.panel is not None else None
            except Exception:
                pass
            side = "🟢 买入" if "buy" in str(o["side"]).lower() else "🔴 卖出"
            lines.append(f"| {side} | {o['symbol']} | {int(o['quantity'])} | {px if px else '-'} | {o.get('reason', '')} |")
        lines.append("")
        lines.append("> 参考现价为今日收盘；明日以开盘价撮合，涨跌停封板/资金不足可能被拒单。")
    if len(pos):
        lines.append("")
        lines.append("## 当前持仓")
        lines.append("")
        lines.append("| 标的 | 股数 | 权重 |")
        lines.append("|---|---|---|")
        w: pd.DataFrame = sig.get("weights", pd.DataFrame())
        for _, row in pos.iterrows():
            pct = ""
            if w is not None and len(w) and row["标的"] in w.columns:
                pct = f"{float(w.iloc[-1][row['标的']]):.1%}"
            lines.append(f"| {row['标的']} | {int(row['持仓股数'])} | {pct} |")
    if note:
        lines += ["", f"> {note}"]
    # 台账偏离：若该策略记了实际成交，日报里告诉你真实仓位跟策略目标差多少
    try:
        if name:
            from .ledger import load_ledger, positions_from_entries, rebalance_gap

            _ent = load_ledger(name)
            if _ent:
                _tgt = {str(r["标的"]): int(r["持仓股数"]) for _, r in pos.iterrows()}
                if len(ord_):
                    for _, o in ord_.iterrows():
                        _s2, _q2 = str(o["symbol"]), int(o["quantity"])
                        _tgt[_s2] = _tgt.get(_s2, 0) + (_q2 if "buy" in str(o["side"]).lower() else -_q2)
                _g = rebalance_gap(positions_from_entries(_ent), {k: v for k, v in _tgt.items() if v > 0})
                lines += ["", "## ⚖️ 台账实际 vs 策略目标"]
                if len(_g):
                    lines += ["", "| 标的 | 目标 | 实际 | 差额 | 动作 |", "|---|---|---|---|---|"]
                    lines += [f"| {r.symbol} | {int(r.策略目标)} | {int(r.台账实际)} | {int(r.差额):+d} | {r.动作} |" for r in _g.itertuples()]
                    lines += ["", "> 台账若长期偏离，信号已不反映你的真实仓位，建议补齐或重建基准。"]
                else:
                    lines += ["", "✅ 台账与策略目标一致，无需纠偏。"]
    except Exception:
        pass  # 台账只是增值信息，坏了不阻断日报
    return "\n".join(lines)


def run_signal_for_name(name: str, codes: Optional[List[str]] = None, start: str = "2021-09-01",
                        initial_cash: float = 1_000_000.0, out_dir: Optional[str] = None,
                        allow_exec: bool = False) -> str:
    """完整流程：存档 → 数据 → 信号 → 写 Markdown。返回报告文本。

    out_dir 不传时按项目根解析 results/signals（不依赖 cwd，定时任务从任意目录调用结果一致）。
    """
    from quant_sim.core.config import BacktestConfig
    from quant_sim.strategies.store import build_saved, load_strategy

    saved = load_strategy(name)
    if codes is None:
        codes = saved.data_symbols or None
    if not codes:
        raise ValueError("存档没记标的池且未传 --codes；信号需要明确数据范围")
    from quant_sim.data.hithink import load_panel as ht_load

    panel = ht_load(list(codes), start=start, adjust="qfq", auto=True)
    strategy = build_saved(saved, universe=panel.symbols, allow_exec=allow_exec)
    cfg = BacktestConfig(start_date=start, initial_cash=initial_cash,
                         benchmark="000300" if "000300" in panel.symbols else None)
    sig = compute_signals(strategy, panel, cfg)
    sig["weights"] = sig["result"].weights
    report = signal_report(sig, name=saved.name, note=saved.note)
    out_dir = _paths.resolve(out_dir) if out_dir else _paths.signals_dir()
    os.makedirs(out_dir, exist_ok=True)
    d = pd.Timestamp(sig["date"]).strftime("%Y-%m-%d")
    safe = "".join(ch for ch in saved.name if ch.isalnum() or ch in "_-\u4e00-\u9fff")[:40]
    path = os.path.join(out_dir, f"{d}_{safe}.md")
    save_text_atomic(path, report)
    print(report)
    print(f"\n已保存 → {path}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="signals", description="每日盘后信号")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出存档策略")
    rp = sub.add_parser("run", help="对某存档跑今日信号")
    rp.add_argument("--name", required=True)
    rp.add_argument("--codes", nargs="*", default=None)
    rp.add_argument("--start", default="2021-09-01")
    rp.add_argument("--cash", type=float, default=1_000_000.0)
    rp.add_argument("--yes-run-code", dest="allow_exec", action="store_true",
                    help="允许执行代码类存档（分享 JSON=可执行代码，仅在你信任来源时加）")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        from quant_sim.strategies.store import list_strategies

        rows = list_strategies()
        if not rows:
            print("（还没有存档策略：去网页工坊点「保存」或用 store.save_strategy）")
        for s in rows:
            print(f"- {s.name}  [{s.kind}]  更新 {s.updated}  {s.note[:40]}")
    else:
        run_signal_for_name(args.name, codes=args.codes, start=args.start, initial_cash=args.cash,
                            allow_exec=args.allow_exec)


if __name__ == "__main__":
    main()
