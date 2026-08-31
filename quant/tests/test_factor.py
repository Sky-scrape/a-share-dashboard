"""因子工作台统计核测试：合成动量延续面板上 IC/分层/表达式/护栏。"""
import numpy as np
import pandas as pd
import pytest

from quant_sim.core.types import BarPanel
from quant_sim.research.factor import eval_expression, matrices, run_factor_study


def _momentum_panel(seed=7, n_sym=12, n_days=300):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    frames = {}
    for i in range(n_sym):
        r = np.zeros(n_days)
        for t in range(21, n_days):
            r[t] = 0.25 * r[t - 20 : t].mean() + 0.012 * rng.standard_normal()
        close = 10 * np.exp(np.cumsum(r))
        frames[f"S{i:02d}"] = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1e6,
                "amount": close * 1e6,
                "pre_close": np.r_[close[0], close[:-1]],
            },
            index=dates,
        )
    return BarPanel.from_wide(frames)


def test_momentum_factor_detects_persistence():
    res = run_factor_study(_momentum_panel(), ["mom_20"])
    s = res["summary"].loc["20日动量"]
    assert s["IC均值"] > 0.03          # 方向正确
    assert s["t值"] > 2.0              # 统计显著
    assert s["有效天数"] > 200
    ls = res["quantile_nav"]["LS(Q高-Q低)"].dropna()
    assert ls.iloc[-1] > 1.1           # 因子高组跑赢低组
    # 持有期越长 IC 不衰减（延续性设计使然）
    hor = res["horizon"]
    assert hor.loc[5, "IC均值"] >= hor.loc[1, "IC均值"] * 0.8


def test_expression_eval_and_guards():
    panel = _momentum_panel(n_sym=8, n_days=200)
    mats = matrices(panel)
    out = eval_expression("rank(mom_20) - rank(vol_20)", mats)
    assert isinstance(out, pd.DataFrame) and out.shape == mats["close"].shape
    with pytest.raises(ValueError):
        eval_expression("__import__('os').system('dir')", mats)
    with pytest.raises(ValueError):
        eval_expression("mom_9999", mats)  # 未知因子名在 eval 内 NameError → ValueError


def test_small_sample_warning():
    res = run_factor_study(_momentum_panel(n_sym=4), ["mom_20"])
    assert any("横截面" in w for w in res["warnings"])


def test_fwd_ret_entry_discipline():
    """t1_open 口径：T 日收盘数据变化不得污染 T 日的未来收益（隔夜段不可交易）。"""
    from quant_sim.research.factor import _fwd_ret

    panel = _momentum_panel(n_sym=6, n_days=120)
    mats = matrices(panel)
    r_open = _fwd_ret(mats, 1, entry="t1_open")
    # 对齐性：k=1 时 = open[T+2]/open[T+1] - 1
    o = mats["open"]
    assert np.isclose(r_open.iloc[10, 3], o.iloc[12, 3] / o.iloc[11, 3] - 1)
    # T 日收盘被改 5%：t1_open 收益不变，旧 t_close 口径会变（对照）
    c2 = mats["close"].copy()
    c2.iloc[40, 0] *= 1.05
    mats2 = {**mats, "close": c2}
    assert np.isclose(_fwd_ret(mats2, 1, entry="t1_open").iloc[40, 0], r_open.iloc[40, 0])
    assert not np.isclose(_fwd_ret(mats2, 1, entry="t_close").iloc[40, 0],
                          _fwd_ret(mats, 1, entry="t_close").iloc[40, 0])
    with pytest.raises(ValueError):
        _fwd_ret(mats, 1, entry="magic")


def test_suspend_days_masked():
    """插值停牌日（volume=0）不得参与截面排名。"""
    panel = _momentum_panel(n_sym=8, n_days=120)
    mats = matrices(panel)
    arr = panel.arrays["volume"].copy()
    arr[30:60, 2] = 0.0  # 制造一段停牌
    from quant_sim.core.types import BarPanel as BP

    import numpy as _np
    panel2 = BP(panel.dates, panel.symbols, {**panel.arrays, "volume": arr})
    mats2 = matrices(panel2)
    # 停牌日（volume=0）的 open 被 mask 为 NaN，不参与截面
    assert np.isnan(mats2["open"].iloc[40, 2])
    assert not np.isnan(mats["open"].iloc[40, 2])  # 对照组：原面板同日有效
