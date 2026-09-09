"""回测结果导出：CSV + JSON + 自包含 HTML 报告。

HTML 模板在 `templates/report.html`（与代码分离，改样式不碰 Python）；报告内嵌数据与
ECharts（CDN），离线打开时图表降级为占位提示，表格部分仍可完整阅读。
所有注入模板的用户可控文本（标题/备注/键名）统一过 HTML 转义。
"""

from __future__ import annotations

import html as _html
import json
import math
import os
import re
from dataclasses import asdict
from functools import lru_cache
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd


def _esc(v) -> str:
    return _html.escape(str(v), quote=False)


@lru_cache(maxsize=1)
def _template() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "report.html")
    with open(path, encoding="utf-8") as f:
        return f.read()



def _table(df: pd.DataFrame, max_rows: int = 200) -> str:
    if df is None or df.empty:
        return "<p class='note'>（无记录）</p>"
    d = df.head(max_rows).copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else f"{v:,.4f}".rstrip("0").rstrip("."))
        elif pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.date.astype(str)

    esc = _esc

    head = "".join(f"<th>{esc(c)}</th>" for c in d.columns)
    body = "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>" for row in d.itertuples(index=False))
    more = f"<p class='note'>共 {len(df)} 条，仅显示前 {max_rows} 条</p>" if len(df) > max_rows else ""
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"


def _cards(metrics: dict) -> str:
    keys = ["累计收益率", "年化收益率", "最大回撤", "夏普比率", "胜率", "交易次数", "总手续费", "期末权益"]
    html = []
    for k in keys:
        if k not in metrics:
            continue
        v = metrics[k]
        html.append(f'<div class="card"><div class="k">{_esc(k)}</div><div class="v">{_card_value(k, v)}</div></div>')
    return "".join(html)


def _card_value(key: str, v) -> str:
    from .formatter import display_value, is_percent_key

    text = display_value(key, v, money_unit=False)
    # HTML 卡片特有的红涨绿跌语义（仅百分比类指标上色）；数字规则不在此重复。
    if is_percent_key(key) and isinstance(v, (int, float)) and v == v and not (isinstance(v, float) and math.isinf(v)):
        cls = "pos" if v >= 0 else "neg"
        return f'<span class="{cls}">{text}</span>'
    return text


def save_result(
    result,
    out_dir: str = "results",
    name: str = "backtest",
    formats: Sequence[str] = ("csv", "json", "html"),
    data_note: str = "",
) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths: List[str] = []

    def subdir(kind: str) -> str:
        d = os.path.join(out_dir, f"{name}_{kind}")
        os.makedirs(d, exist_ok=True)
        return d

    if "csv" in formats:
        d = subdir("csv")
        result.equity.rename("equity").to_frame().join(result.cash.rename("cash")).join(
            result.holdings_value.rename("holdings_value")
        ).to_csv(os.path.join(d, "equity_curve.csv"), encoding="utf-8-sig")
        result.drawdown.rename("drawdown").to_csv(os.path.join(d, "drawdown.csv"), encoding="utf-8-sig")
        if not result.trades.empty:
            result.trades.to_csv(os.path.join(d, "trades.csv"), index=False, encoding="utf-8-sig")
        if not result.fills.empty:
            result.fills.to_csv(os.path.join(d, "fills.csv"), index=False, encoding="utf-8-sig")
        if not result.orders.empty:
            result.orders.to_csv(os.path.join(d, "orders.csv"), index=False, encoding="utf-8-sig")
        result.metrics_df.to_csv(os.path.join(d, "metrics.csv"), index=False, encoding="utf-8-sig")
        result.weights.to_csv(os.path.join(d, "weights.csv"), encoding="utf-8-sig")
        paths.append(d)

    if "json" in formats:
        path = os.path.join(out_dir, f"{name}_metrics.json")
        payload = {
            "metrics": {k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v) for k, v in result.metrics.items()},
            "config": _jsonable(asdict(result.config)),
            "risk_events": [_jsonable(asdict(e)) for e in result.risk_events],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        paths.append(path)

    if "html" in formats:
        eq = result.equity
        dd = result.drawdown
        bench = result.benchmark
        bench_series = None
        if bench is not None:
            aligned = bench.reindex(eq.index).ffill()
            base = aligned.dropna()
            if len(base):
                bench_series = (aligned / base.iloc[0] * eq.iloc[0]).round(4).to_list()
        data = {
            "dates": [str(d.date()) for d in eq.index],
            "equity": eq.round(4).to_list(),
            "drawdown": (dd * 100).round(3).to_list(),
            "benchmark": bench_series,
        }
        trades = result.trades
        if not trades.empty and "entry_date" in trades.columns:
            trades = trades.sort_values("exit_date", ascending=False)
        rejects = result.orders[~result.orders["status"].isin(["filled", "partial"])] if not result.orders.empty else result.orders
        positions = _latest_positions(result)
        _note = data_note or ("⚠️ 合成演示数据" if result.panel and result.panel.metadata.get("kind") == "synthetic" else "")
        html = (
            _template()
            .replace("__TITLE__", _esc(f"回测报告 · {name}"))
            .replace("__GEN_TIME__", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__EXEC__", _esc(result.config.execution))
            .replace("__DATA_NOTE__", _esc(_note))
            .replace("__CARDS__", _cards(result.metrics))
            .replace("__METRICS_TABLE__", _table(result.metrics_df, 100))
            .replace("__POSITIONS_TABLE__", _table(positions, 50))
            .replace("__TRADES_TABLE__", _table(trades, 200))
            .replace("__REJECTS_TABLE__", _table(rejects, 100))
            .replace("__DATA__", json.dumps(data, default=str))
        )
        path = os.path.join(out_dir, f"{name}_report.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        paths.append(path)
    return paths


def _latest_positions(result) -> pd.DataFrame:
    last_pos = result.positions.iloc[-1]
    last_price = {}
    if result.panel is not None:
        bars = result.panel.bars(result.dates[-1])
        last_price = {s: b.close for s, b in bars.items()}
    rows = []
    for symbol, qty in last_pos.items():
        if qty <= 0:
            continue
        price = last_price.get(symbol, 0.0)
        rows.append({"symbol": symbol, "quantity": int(qty), "close": round(price, 4), "market_value": round(qty * price, 2)})
    return pd.DataFrame(rows)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return obj
