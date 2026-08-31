"""策略存档 / 盘后信号 / 归因分析 / 多策略对比测试。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from quant_sim.core.config import BacktestConfig
from quant_sim.core.engine import run_backtest
from quant_sim.data.sample import make_demo_panel
from quant_sim.strategies.store import (
    SavedStrategy,
    build_saved,
    delete_strategy,
    list_strategies,
    load_strategy,
    save_strategy,
    strategy_description,
)


def _saved(kind, payload, name="x"):
    return SavedStrategy(name=name, kind=kind, payload=payload)


@pytest.fixture(scope="module")
def panel():
    return make_demo_panel(seed=11)


@pytest.fixture(scope="module")
def cfg():
    return BacktestConfig(start_date="2023-01-01", end_date="2025-12-31", benchmark="510300")


RULE_SPEC = {
    "symbols": ["510300"],
    "entry": {"mode": "all", "conds": [
        {"left": {"t": "ind", "i": "sma", "p": {"n": 5}, "c": ""}, "op": "cross_above",
         "right": {"t": "ind", "i": "sma", "p": {"n": 20}, "c": ""}}]},
    "exit": {"mode": "any", "conds": []},
    "target_weight": 0.9, "stop_loss_pct": 0.08,
}


# ---------------------------------------------------------------- store
def test_store_roundtrip(tmp_path):
    save_strategy("测试甲", "rule", RULE_SPEC, note="n1", data_symbols=["510300"], store_dir=str(tmp_path))
    rows = list_strategies(str(tmp_path))
    assert [r.name for r in rows] == ["测试甲"]
    s = load_strategy("测试甲", store_dir=str(tmp_path))
    assert s.kind == "rule" and s.payload["target_weight"] == 0.9
    assert "510300" in strategy_description(s)
    # JSON 文件可移植：内容合法且可重建策略
    doc = json.loads((tmp_path / "测试甲.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "rule"
    strat = build_saved(s)
    r = run_backtest(strat, make_demo_panel(seed=11), BacktestConfig(start_date="2023-01-01"))
    assert r.metrics["交易次数"] >= 0
    # 覆盖与删除
    save_strategy("测试甲", "builtin", {"family": "meanrev", "params": {"symbol": "510300"}}, store_dir=str(tmp_path))
    assert load_strategy("测试甲", store_dir=str(tmp_path)).kind == "builtin"
    assert delete_strategy("测试甲", store_dir=str(tmp_path))
    assert list_strategies(str(tmp_path)) == []


def test_store_bad_input(tmp_path):
    with pytest.raises(ValueError):
        save_strategy("x", "unknown_kind", {}, store_dir=str(tmp_path))
    with pytest.raises(ValueError):
        save_strategy("x", "rule", {"bad": {1, 2}}, store_dir=str(tmp_path))  # 非 JSON 可序列化
    with pytest.raises(FileNotFoundError):
        load_strategy("不存在", store_dir=str(tmp_path))
    with pytest.raises(ValueError):
        build_saved(_saved("alien", {}))


def test_store_builtin_and_code(tmp_path):
    save_strategy("b", "builtin", {"family": "dual_ma", "params": {"symbol": "510300", "fast": 5, "slow": 20}}, store_dir=str(tmp_path))
    st1 = build_saved(load_strategy("b", store_dir=str(tmp_path)))
    assert st1.fast == 5
    save_strategy("c", "code", {"code": "class MyStrategy(Strategy):\n    def on_bar(self, ctx):\n        pass\n"}, store_dir=str(tmp_path))
    # 代码存档默认拒绝回放（分享 JSON=可执行代码）；显式 allow_exec 才过闸
    with pytest.raises(PermissionError, match="allow_exec|yes-run-code"):
        build_saved(load_strategy("c", store_dir=str(tmp_path)))
    st2 = build_saved(load_strategy("c", store_dir=str(tmp_path)), allow_exec=True)
    assert type(st2).__name__ == "MyStrategy"


# ---------------------------------------------------------------- signals
def test_signals_keep_positions_and_pending(panel):
    from quant_sim.tools.signals import compute_signals, signal_report

    cfg_full = BacktestConfig(start_date="2023-01-01", end_date="2025-12-31", benchmark="510300")
    strat = build_saved(_saved("rule", RULE_SPEC))
    sig = compute_signals(strat, panel, cfg_full)
    # liquidate_on_end=False → 末日可能仍有持仓；对照默认回测的 result 期末 equity 被拍平
    assert sig["date"] == panel.dates[-1]
    assert sig["equity"] > 0
    assert set(sig["orders"].columns) >= {"symbol", "side", "quantity"}
    rep = signal_report({**sig, "weights": sig["result"].weights}, name="信号测试")
    assert "盘后信号" in rep and str(sig["date"].date()) in rep


def test_liquidate_flag_respected(panel, cfg):
    # 恒真入场且无出场 → 期末必持仓
    hold_spec = {
        "symbols": ["510300"],
        "entry": {"mode": "all", "conds": [{"left": {"t": "field", "f": "open"}, "op": "gt", "right": {"t": "const", "v": -1}}]},
        "exit": {"mode": "any", "conds": []},
        "target_weight": 0.5,
    }
    from dataclasses import replace

    r_flat = run_backtest(build_saved(_saved("rule", hold_spec)), panel, cfg)
    r_hold = run_backtest(build_saved(_saved("rule", hold_spec)), panel, replace(cfg, liquidate_on_end=False))
    flat_force = sum("强制平仓" in str(x) for x in r_flat.fills["reason"]) if len(r_flat.fills) else 0
    hold_force = sum("强制平仓" in str(x) for x in r_hold.fills["reason"]) if len(r_hold.fills) else 0
    assert flat_force > 0 and hold_force == 0


# ---------------------------------------------------------------- attribution
def test_attribution_outputs(panel, cfg):
    from quant_sim.metrics.attribution import cost_drag, holding_stats, monthly_returns, trade_leaderboard
    from quant_sim.strategies import MeanReversionStrategy

    r = run_backtest(MeanReversionStrategy("510300"), panel, cfg)
    mr = monthly_returns(r.equity)
    assert list(mr.columns)[-1] == "全年" and mr.shape[0] >= 2
    hs = holding_stats(r.trades)
    assert len(hs["summary"]) == 5 and hs["buckets"]["交易数"].sum() == len(r.trades)
    cd = cost_drag(r)
    assert cd.iloc[-1]["金额"] >= 0 and cd["项目"].tolist()[-1] == "合计"
    lb = trade_leaderboard(r.trades, n=3)
    assert len(lb["best"]) <= 3 and lb["by_symbol"]["合计盈亏"].sum() == pytest.approx(r.trades["net_pnl"].sum())


# ---------------------------------------------------------------- compare
def test_compare_portfolio_math(panel, cfg):
    from quant_sim.research.compare import compare_strategies
    from quant_sim.strategies import MeanReversionStrategy

    out = compare_strategies(
        {"双": lambda: build_saved(_saved("rule", RULE_SPEC)),
         "均": lambda: MeanReversionStrategy("510300")},
        panel, cfg,
    )
    assert set(out["normalized"].columns) == {"双", "均"}
    tbl = out["table"]
    assert "★ 等权组合" in tbl["策略"].values
    # 组合起点 = 平均初始资金；曲线覆盖同一日期轴
    port = out["portfolio_curve"]
    assert abs(port.iloc[0] - out["normalized"].iloc[0].mul(0).add(1).mul(1e6).mean()) < 1e6 * 0.001 + 1  # 1,000,000 起点
    assert len(port) == len(out["normalized"])
    assert out["return_corr"].shape == (2, 2)
    # 组合回撤 ≤ 两策略回撤中较大者（等权平滑效应，一般成立）
    row = tbl[tbl["策略"] == "★ 等权组合"].iloc[0]
    assert row["最大回撤"] <= max(tbl[tbl["策略"] != "★ 等权组合"]["最大回撤"]) + 1e-9


# ============================================================ PM 进阶包回归
def test_store_version_increment(tmp_path):
    """覆盖保存：version 自增、created 保留。"""
    p1 = save_strategy("版本测试", "builtin", {"family": "dual_ma", "params": {"symbol": "600000", "fast": 5, "slow": 20}},
                       note="v1", store_dir=str(tmp_path))
    s1 = load_strategy("版本测试", store_dir=str(tmp_path))
    assert s1.version == 1 and s1.created
    import time as _t
    _t.sleep(0.01)
    save_strategy("版本测试", "builtin", {"family": "dual_ma", "params": {"symbol": "600000", "fast": 10, "slow": 30}},
                  note="v2", store_dir=str(tmp_path))
    s2 = load_strategy("版本测试", store_dir=str(tmp_path))
    assert s2.version == 2
    assert s2.created == s1.created  # 首次创建时间保留
    assert s2.note == "v2"


def test_sensitivity_perturb():
    from quant_sim.research.sensitivity import perturb_variants

    spec = {
        "symbols": ["600000"],
        "entry": {"mode": "all", "conds": [
            {"left": {"t": "ind", "i": "sma", "p": {"n": 10}, "c": ""}, "op": "cross_above",
             "right": {"t": "ind", "i": "sma", "p": {"n": 30}, "c": ""}}]},
        "exit": {"mode": "all", "conds": []},
    }
    vs = perturb_variants(spec)
    assert len(vs) >= 2
    labels = [l for l, _ in vs]
    assert len(set(labels)) == len(labels), "同名参数多处出现时标签必须可区分"
    # 原对象绝不被改动（深拷贝）
    assert spec["entry"]["conds"][0]["left"]["p"]["n"] == 10
    # 变体值确实是 ±20% 邻域
    news = {v["entry"]["conds"][0]["left"]["p"]["n"] for _, v in vs if v["entry"]["conds"][0]["left"]["p"]["n"] != 10
            or v["entry"]["conds"][0]["right"]["p"]["n"] != 30}
    assert any(x in (8, 12) for x in
               [v["entry"]["conds"][0]["left"]["p"]["n"] for _, v in vs])


def test_ledger_positions_and_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_SIM_LEDGER", str(tmp_path))
    from quant_sim.tools import ledger as L

    L.add_entry("t", "159915", "buy", 1000, 2.35, date="2026-08-20")
    L.add_entry("t", "159915", "buy", 500, 2.50, date="2026-08-21")
    L.add_entry("t", "510300", "buy", 200, 4.10, date="2026-08-21")
    L.add_entry("t", "510300", "sell", 100, 4.20, date="2026-08-25")
    pos = L.positions_from_entries(L.load_ledger("t"))
    idx = pos.set_index("symbol")
    assert idx.loc["159915", "qty"] == 1500
    assert abs(idx.loc["159915", "avg_cost"] - (1000 * 2.35 + 500 * 2.5) / 1500) < 1e-9
    # 卖超截断 + 警告
    L.add_entry("t", "510300", "sell", 9999, 4.0, date="2026-08-26")
    p2 = L.positions_from_entries(L.load_ledger("t"))
    assert p2.attrs["warnings"] and (p2["symbol"] == "510300").sum() == 0
    # 市值表：缺价回退成本
    mtm = L.mark_to_market(pos, {"159915": 2.60})
    row = mtm.set_index("symbol").loc["510300"]
    assert row["last"] == row["avg_cost"] and abs(row["浮盈"]) < 1e-9
    # 差额清单
    gap = L.rebalance_gap(pos, {"159915": 2000, "588000": 300})
    assert set(gap["symbol"]) == {"159915", "510300", "588000"}
    assert gap[gap["symbol"] == "159915"]["动作"].iloc[0] == "买入"
    assert L.undo_last("t") is not None
