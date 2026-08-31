"""多策略对比与组合：各策略独立资金跑一遍，净值叠加 + 等权组合曲线。

组合口径说明：等权组合 = 各策略**日收益率**等权平均后累乘，等价于每天把资金均分
给每个策略、每日再平衡；不是共享一个账户撮合（共享账户会有资金互相挤占的次序效应，
那是模拟盘 M3 的命题）。对比表里给出各策略与组合的关键指标。
"""

from __future__ import annotations

from typing import Callable, Dict

import numpy as np
import pandas as pd

from ..core.config import BacktestConfig
from .runner import COMPARE_METRICS, run_variants


def compare_strategies(
    named_factories: Dict[str, Callable[[], "object"]],
    panel,
    config: BacktestConfig,
) -> Dict[str, object]:
    """named_factories: {展示名: 无参工厂(返回 Strategy)}。返回 results/equity 矩阵/对比表/组合。

    批跑循环已收敛到 research.runner.run_variants（与网格/WF/敏感性同一实现）；
    失败策略不再 print 到 stdout，统一进 _error 列供 UI 展示。
    """
    from ..metrics.performance import compute_metrics

    variants = [{"策略": name} for name in named_factories]
    table_raw, results = run_variants(
        variants,
        build=lambda v: named_factories[v["策略"]](),
        panel=panel,
        config=config,
        metrics=COMPARE_METRICS,
        lite=False,
    )
    curves = {}
    rows = []
    for rec, r in zip(table_raw.to_dict("records"), results):
        if r is None:
            continue
        curves[rec["策略"]] = r.equity
        rows.append({
            "策略": rec["策略"],
            "累计收益率": rec.get("累计收益率"), "年化收益率": rec.get("年化收益率"),
            "最大回撤": rec.get("最大回撤"), "夏普比率": rec.get("夏普比率"),
            "卡玛比率": rec.get("卡玛比率"), "交易次数": rec.get("交易次数"),
            "期末权益": float(r.equity.iloc[-1]),
        })
    if not curves:
        raise ValueError("所有策略都失败了，无对比结果")
    eq_mat = pd.DataFrame(curves)
    eq_mat = eq_mat.dropna(how="all").ffill()
    norm = eq_mat / eq_mat.iloc[0]

    rets = eq_mat.pct_change().dropna(how="all").fillna(0.0)
    port_ret = rets.mean(axis=1)
    avg_start = float(eq_mat.iloc[0].mean())
    port = (1 + port_ret).cumprod() * avg_start
    port = pd.concat([pd.Series([avg_start], index=[eq_mat.index[0]]), port])
    port.name = "等权组合"

    pm = compute_metrics(
        pd.DataFrame({"equity": port, "cash": port, "holdings_value": 0.0}), [], account=None, config=config
    )
    rows.append({
        "策略": "★ 等权组合",
        "累计收益率": pm.get("累计收益率"), "年化收益率": pm.get("年化收益率"),
        "最大回撤": pm.get("最大回撤"), "夏普比率": pm.get("夏普比率"),
        "卡玛比率": pm.get("卡玛比率"), "交易次数": None, "期末权益": float(port.iloc[-1]),
    })
    table = pd.DataFrame(rows).sort_values("夏普比率", ascending=False).reset_index(drop=True)

    # 两两净值相关性（日收益）：低相关才有组合价值
    corr = rets.corr().round(3)

    return {
        "results": {rec["策略"]: r for rec, r in zip(table_raw.to_dict("records"), results) if r is not None},
        "failures": {str(rec["策略"]): rec["_error"] for rec in table_raw.to_dict("records") if rec["_error"]},
        "normalized": norm,
        "portfolio_curve": port,
        "table": table,
        "return_corr": corr,
    }
