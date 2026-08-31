"""条件选股器测试：合成行情因子 + monkeypatch 远端调用（不碰真实 CLI/DB）。"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quant_sim.research import screener


# ------------------------------------------------------------------ 合成全市场长表
def _mk_long(n_days: int = 150, seed: int = 7) -> pd.DataFrame:
    """3 只形态截然不同的股票：趋势多头 / 单边下跌 / 横盘。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_days).strftime("%Y-%m-%d")
    rows = []

    def add(ticker: str, closes: np.ndarray, amount: float):
        for d, c in zip(dates, closes):
            rows.append({"thscode": f"{ticker}.SH" if ticker[0] == "6" else f"{ticker}.SZ",
                         "ticker": ticker, "date": d, "open": c * 0.999, "high": c * 1.002,
                         "low": c * 0.997, "close": c, "volume": amount / max(c, 1e-9), "amount": amount})

    trend = 10 * np.linspace(1, 1.6, n_days) + rng.normal(0, 0.01, n_days).cumsum() * 0.1
    downt = 20 * np.linspace(1, 0.6, n_days)
    flat = np.full(n_days, 5.0) + rng.normal(0, 0.005, n_days)
    add("600001", trend, 3e8)   # 主板·强势上行：多头排列+新高
    add("000002", downt, 5e7)   # 主板·阴跌：MA 下方、深回撤
    add("300003", flat, 2e6)    # 创业板·横盘缩量：流动性差
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def factors():
    f = screener._compute_factors(_mk_long())
    f.attrs["asof"] = "2026-08-28"
    f.attrs["window_days"] = 150
    return f


def test_board_of():
    assert screener.board_of("600519") == "主板"
    assert screener.board_of("300750") == "创业板"
    assert screener.board_of("688981") == "科创板"
    assert screener.board_of("920002") == "北交所"
    assert screener.to_thscode("600519") == "600519.SH"
    assert screener.to_thscode("000001") == "000001.SZ"
    assert screener.to_thscode("688981") == "688981.SH"


def test_factors_shapes(factors):
    assert set(["close", "chg_1d", "ma_bull", "mom_20", "dd_60", "vol_ratio", "amt_20", "hist_days"]) <= set(factors.columns)
    assert factors.loc["600001", "ma_bull"] is True or bool(factors.loc["600001", "ma_bull"])
    assert float(factors.loc["600001", "mom_20"]) > 0
    assert not bool(factors.loc["000002", "ma_bull"])
    assert float(factors.loc["000002", "dd_60"]) < -0.15   # 阴跌股距 60 日高点回撤深
    assert bool(factors.loc["600001", "new_high_60"])


def test_screen_local_only_no_enrich(factors):
    res = screener.screen(factors, boards=["主板"], bools=["ma_bull"], enrich=False)
    assert res["steps"]["enriched"] == 0
    assert set(res["table"]["ticker"]) == {"600001"}
    assert res["asof"] == "2026-08-28"
    # pct 列已换算为百分数显示
    assert float(res["table"].iloc[0]["mom_20"]) > 0


def test_screen_range_units_and_limit(factors):
    # mom_20 以 % 输入（scale=100）：要求 20日涨幅 ≥ 5%
    res = screener.screen(factors, ranges={"mom_20": (5, None)}, enrich=False)
    assert "600001" in set(res["table"]["ticker"])
    assert "000002" not in set(res["table"]["ticker"])
    # amt_20 以 亿 输入：横盘票 200 万/日 → 0.02 亿，被 1 亿门槛挡掉
    res2 = screener.screen(factors, ranges={"amt_20": (1.0, None)}, enrich=False)
    assert "300003" not in set(res2["table"]["ticker"])
    res3 = screener.screen(factors, limit=2, enrich=False)
    assert len(res3["table"]) <= 2


def test_screen_universe_filter(factors):
    res = screener.screen(factors, universe=["600001", "999999"], enrich=False)
    assert res["steps"]["pool"] == 1
    assert any("股票池" in w for w in res["warnings"]) or len(res["table"]) == 1


def test_screen_valuation_enrich(monkeypatch, factors):
    fake = pd.DataFrame(
        {"name": ["上升强股", "ST阴跌", "横盘"], "pe_ttm": [10.0, 8.0, 30.0], "pb_mrq": [1.5, 0.8, 3.0]},
        index=["600001", "000002", "300003"])
    monkeypatch.setattr(screener, "attach_valuation", lambda t, pause=0.15: fake.reindex(list(t)))
    # PE<15 且排除 ST：000002 PE 达标但名称含 ST → 出局；300003 PE 30 → 出局
    res = screener.screen(factors, bools=["exclude_st"], ranges={"pe_ttm": (0.1, 15)})
    assert set(res["table"]["ticker"]) == {"600001"}
    assert "name" in res["table"].columns  # 结果表带名称列（DISPLAY_COLS 渲染为「名称」）
    assert (res["table"]["name"] == "上升强股").all()


def test_screen_valuation_missing_column_warning(monkeypatch, factors):
    monkeypatch.setattr(screener, "attach_valuation", lambda t, pause=0.15: pd.DataFrame({"name": ["某股"]}, index=["600001"]).reindex(list(t)))
    res = screener.screen(factors[["close", "chg_1d", "amt_20", "board"]].assign(
        above_ma20=True, above_ma60=True, above_ma120=True, ma_bull=True, new_high_60=True,
        mom_5=0.0, mom_20=0.0, mom_60=0.0, mom_120=0.0, dd_60=0.0, vol_ratio=1.0, vol_ann_20=1.0,
        upday_20=0.5, hist_days=150), ranges={"pb_mrq": (0, 5)})
    assert any("pb_mrq" in w for w in res["warnings"])


def test_screen_unknown_condition(factors):
    with pytest.raises(KeyError):
        screener.screen(factors, bools=["no_such_cond"])


def test_templates_reference_valid_conditions():
    for name, tpl in screener.TEMPLATES.items():
        for k in tpl.get("bools", []):
            assert k in screener.CONDITIONS, f"{name} 引用未知条件 {k}"
        for k, v in tpl.get("ranges", {}).items():
            assert k in screener.CONDITIONS, f"{name} 引用未知条件 {k}"
            assert screener.CONDITIONS[k]["kind"] == "range"
            assert isinstance(v, tuple) and len(v) == 2


def test_conditions_registry():
    for k, c in screener.CONDITIONS.items():
        assert c["group"] in screener.GROUPS
        assert c["kind"] in ("range", "bool")
        if c["kind"] == "range":
            assert c["unit"] in ("%", "元", "亿", "倍", "天")
            assert "col" in c and "apply" in c
    # 估值条件必须标记 valuation=True（否则会被当本地条件跑）
    for k in ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "exclude_st"):
        assert screener.CONDITIONS[k].get("valuation")


def test_get_universe_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(screener, "CACHE_DIR", tmp_path)
    today = pd.Timestamp.today().strftime("%Y%m%d")
    items = [{"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
             {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行"}]
    (tmp_path / f"const_000300_{today}.json").write_text(json.dumps(items), encoding="utf-8")
    uni = screener.get_universe("沪深300")
    assert uni == ["600519", "000001"]
    assert screener.get_universe("全市场") is None


def test_max_enrich_cap(monkeypatch, factors):
    calls = []
    fake = pd.DataFrame({"name": ["a", "b", "c"], "pe_ttm": [10.0, 20.0, 30.0]},
                        index=["600001", "000002", "300003"])

    def spy(t, pause=0.15):
        calls.append(len(list(t)))
        return fake.reindex(list(t))

    monkeypatch.setattr(screener, "attach_valuation", spy)
    res = screener.screen(factors, sort_by="amt_20", max_enrich=2)
    assert calls and calls[0] <= 2
    assert any("富化" in w or "上限" in w for w in res["warnings"])
