"""因子工作台：单因子 Rank IC / 五分层 / 多期衰减 / 因子相关性。

定位：横截面筛选研究，只吃**量价数据**（Panel 现成），不做基本面因子
（point-in-time 披露对齐复杂，留给后续）。所有统计只在当日有效截面样本
≥ min_cross 的日子计算，横截面太小（比如只有几只 ETF）时结果仅供参考，
UI 有告警。分层净值是**零成本理想化**的，换手衰减表用来判断费后是否可行。
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

__all__ = ["FACTORS", "factor_matrix", "eval_expression", "run_factor_study"]

# ------------------------------------------------------------------ 宽矩阵
_MAT_COLS = ("open", "high", "low", "close", "pre_close", "volume", "amount")


def matrices(panel) -> Dict[str, pd.DataFrame]:
    """BarPanel → {字段: date×symbol DataFrame}。close 为 NaN 处全字段 NaN。"""
    idx, cols = pd.DatetimeIndex(panel.dates), panel.symbols
    out: Dict[str, pd.DataFrame] = {}
    valid = pd.DataFrame(~np.isnan(panel.arrays["close"]), index=idx, columns=cols)
    for f in _MAT_COLS:
        df = pd.DataFrame(np.where(valid, panel.arrays[f], np.nan), index=idx, columns=cols)
        # 停牌日（量额为 0 的插值填充日）同样视为无效截面点
        if f in ("open", "high", "low", "volume", "amount"):
            df = df.mask(valid & (panel.arrays["volume"] <= 0))
        out[f] = df
    return out


def _fwd_ret(mats: Dict[str, pd.DataFrame], k: int, entry: str = "t1_open") -> pd.DataFrame:
    """未来 k 日收益（逐日横截面矩阵）。

    entry 口径：
      * "t1_open"（默认，与引擎纪律对齐）：T 收盘算信号 → T+1 开盘建仓 → T+1+k 开盘离场，
        即 open.shift(-(k+1)) / open.shift(-1) - 1；
      * "t1_close"：T+1 收盘建仓 → T+1+k 收盘离场（收盘价口径，抹平开盘噪声但多假设一日）；
      * "t_close"（旧口径，仅供对照）：T 收盘起算——含一段实盘拿不到的隔夜收益，
        IC/分层净值会系统性虚高，引擎用 next_open 辛辛苦苦防的乐观偏差正是这个。
    """
    if entry == "t_close":
        c = mats["close"]
        return c.shift(-k) / c - 1.0
    if entry == "t1_close":
        c = mats["close"]
        return c.shift(-(k + 1)) / c.shift(-1) - 1.0
    if entry == "t1_open":
        o = mats["open"]
        return o.shift(-(k + 1)) / o.shift(-1) - 1.0
    raise ValueError(f"未知建仓口径 entry={entry!r}（可选 t1_open | t1_close | t_close）")


# ------------------------------------------------------------------ 因子库
def _mom(n):
    return lambda m: m["close"] / m["close"].shift(n) - 1.0


def _vol(n):
    return lambda m: m["close"].pct_change().rolling(n).std()


def _upday(n):
    return lambda m: (m["close"].pct_change() > 0).rolling(n).mean()


def _amt_ratio(n):
    return lambda m: m["amount"] / m["amount"].rolling(n).mean()


def _amp(n):
    return lambda m: ((m["high"] - m["low"]) / m["pre_close"]).rolling(n).mean()


def _dist_high(n):
    return lambda m: m["close"] / m["high"].rolling(n).max()


def _gap(n):
    return lambda m: ((m["open"] - m["pre_close"]) / m["pre_close"]).rolling(n).mean()


def _amax_premium(n):
    # 20 日最大单日涨幅：彩票偏好代理（A 股经验为负向）
    return lambda m: m["close"].pct_change().rolling(n).max()


FACTORS: Dict[str, dict] = {
    "mom_20":  {"label": "20日动量",   "fn": _mom(20),  "desc": "近 20 日涨幅（Trend）"},
    "mom_60":  {"label": "60日动量",   "fn": _mom(60),  "desc": "近 60 日涨幅"},
    "mom_120": {"label": "120日动量",  "fn": _mom(120), "desc": "近 120 日涨幅"},
    "rev_5":   {"label": "5日反转",    "fn": _mom(5),   "desc": "近 5 日涨幅；A 股短线常为负 IC（反转）"},
    "vol_20":  {"label": "20日波动",   "fn": _vol(20),  "desc": "日收益 20 日标准差"},
    "upday_20": {"label": "20日胜率",  "fn": _upday(20), "desc": "近 20 日上涨天数占比"},
    "amt_ratio_20": {"label": "20日量比", "fn": _amt_ratio(20), "desc": "当日成交额 / 20 日均额"},
    "amp_20":  {"label": "20日振幅",   "fn": _amp(20),  "desc": "高低差/昨收 的 20 日均值"},
    "dist_high_60": {"label": "距60日高点", "fn": _dist_high(60), "desc": "收盘 / 60 日最高，越接近 1 越强"},
    "gap_5":   {"label": "5日开盘溢价", "fn": _gap(5),  "desc": "开盘相对昨收溢价的 5 日均值"},
    "max_ret_20": {"label": "20日最大单日涨幅", "fn": _amax_premium(20), "desc": "彩票偏好代理，经验偏负"},
}

_SAFE_EXPR = re.compile(r"^[0-9a-zA-Z_+\-*/()., ]+$")


def factor_matrix(mats: Dict[str, pd.DataFrame], name: str) -> pd.DataFrame:
    if name not in FACTORS:
        raise KeyError(f"未知因子：{name}")
    return FACTORS[name]["fn"](mats)


def eval_expression(expr: str, mats: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """受限表达式：只允许内置因子名、rank、加减乘除、括号与数字。

    例：``mom_20 - mom_120``（剔长动量的中短期）、``rank(mom_60) * rank(amt_ratio_20)``。
    表达式来自本机用户输入（非网络），用无 builtins 的 eval 限定命名空间。
    """
    expr = expr.strip()
    if not expr or not _SAFE_EXPR.match(expr):
        raise ValueError("表达式含非法字符（只允许因子名/rank/+-*/()数字）")
    ns = {k: factor_matrix(mats, k) for k in FACTORS}
    ns["rank"] = lambda df: df.rank(axis=1, pct=True)
    try:
        out = eval(expr, {"__builtins__": {}}, ns)  # noqa: S307 - 本机输入+受限命名空间
    except Exception as e:  # 转成人话
        raise ValueError(f"表达式求值失败：{e}") from e
    if not isinstance(out, pd.DataFrame):
        raise ValueError("表达式结果不是横截面矩阵")
    return out


# ------------------------------------------------------------------ 统计核
def _row_spearman(a: pd.DataFrame, b: pd.DataFrame, min_cross: int) -> pd.Series:
    """逐日横截面 Spearman（NaN 感知），样本不足日给 NaN。"""
    ra, rb = a.rank(axis=1), b.rank(axis=1)
    mask = ra.notna() & rb.notna()
    x, y = ra.where(mask), rb.where(mask)
    n = mask.sum(axis=1)
    xm = x.sub(x.mean(axis=1), axis=0)
    ym = y.sub(y.mean(axis=1), axis=0)
    num = (xm * ym).sum(axis=1)
    den = np.sqrt((xm**2).sum(axis=1) * (ym**2).sum(axis=1))
    ic = (num / den).where(n >= min_cross)
    return ic.dropna().rename("IC")


def ic_summary(ic: pd.Series) -> dict:
    ic = ic.dropna()
    n = len(ic)
    if n < 3:
        return {"IC均值": np.nan, "ICIR": np.nan, "t值": np.nan, "IC>0占比": np.nan, "有效天数": n}
    mean, std = ic.mean(), ic.std()
    return {
        "IC均值": round(mean, 4),
        "ICIR": round(mean / std, 3) if std > 0 else np.nan,
        "t值": round(mean / std * np.sqrt(n), 2) if std > 0 else np.nan,
        "IC>0占比": round((ic > 0).mean(), 3),
        "有效天数": n,
    }


def quantile_nav(fac: pd.DataFrame, mats: Dict[str, pd.DataFrame], nq: int = 5,
                 min_cross: int = 6, entry: str = "t1_open") -> pd.DataFrame:
    """按因子值分为 nq 组（Q5=因子最高），组内等权持有 1 日，复利净值；LS=Q5-Q1。

    建仓口径由 entry 控制（默认 t1_open，与引擎 next_open 纪律一致）；零成本理想化，只读相对排序。
    """
    r1 = _fwd_ret(mats, 1, entry=entry)
    pct = fac.rank(axis=1, method="first")
    n = pct.notna().sum(axis=1)
    counts = np.expand_dims(n.to_numpy(dtype=float), axis=1)  # (T, 1) 行向量
    with np.errstate(divide="ignore", invalid="ignore"):
        bucket = pd.DataFrame(np.ceil(pct.to_numpy() / counts * nq), index=pct.index, columns=pct.columns)
    bucket = bucket.where(fac.notna())
    out: Dict[str, pd.Series] = {}
    for q in range(1, nq + 1):
        ret = r1.where(bucket == q).mean(axis=1)
        out[f"Q{q}"] = (1 + ret).cumprod().where(n >= min_cross)
    top = r1.where(bucket == nq).mean(axis=1)
    bot = r1.where(bucket == 1).mean(axis=1)
    out["LS(Q高-Q低)"] = (1 + top - bot).cumprod().where(n >= min_cross)
    nav = pd.DataFrame(out, index=fac.index)
    nav = nav.dropna(how="all")
    if nav.empty:
        return nav
    return nav / nav.iloc[0]


def horizon_ic(fac: pd.DataFrame, mats: Dict[str, pd.DataFrame],
               horizons=(1, 5, 10, 20), min_cross: int = 6, entry: str = "t1_open") -> pd.DataFrame:
    rows = []
    for h in horizons:
        ic = _row_spearman(fac, _fwd_ret(mats, h, entry=entry), min_cross)
        s = ic_summary(ic)
        rows.append({"持有期(日)": h, "IC均值": s["IC均值"], "ICIR": s["ICIR"],
                     "t值": s["t值"], "有效天数": s["有效天数"]})
    return pd.DataFrame(rows).set_index("持有期(日)")


def factor_autocorr(fac: pd.DataFrame, lags=(1, 5, 10, 20), min_cross: int = 6) -> pd.Series:
    """因子自相关≈换手率代理：越低说明信号换得越快、费后越难兑现。"""
    rows = {}
    for lg in lags:
        ic = _row_spearman(fac, fac.shift(lg), min_cross)
        rows[f"lag{lg}"] = round(float(ic.mean()), 3) if len(ic.dropna()) else np.nan
    return pd.Series(rows, name="因子自相关")


def cross_factor_corr(facs: Dict[str, pd.DataFrame], min_cross: int = 6) -> pd.DataFrame:
    """因子间日均横截面相关（同名 1.0）。用逐日 Pearson of ranks≈Spearman。"""
    keys = list(facs)
    ranks = {k: v.rank(axis=1) for k, v in facs.items()}
    m = pd.DataFrame(np.eye(len(keys)), index=keys, columns=keys)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            m.loc[a, b] = m.loc[b, a] = round(float(_row_spearman(ranks[a], ranks[b], min_cross).mean()), 3)
    return m


# ------------------------------------------------------------------ 主入口
def run_factor_study(panel, keys: List[str], expr: Optional[str] = None,
                     nq: int = 5, min_cross: int = 6,
                     horizons=(1, 5, 10, 20), entry: str = "t1_open") -> dict:
    """一次跑全：返回 {factors, ic, summary(DataFrame), horizon, quantile, autocorr,
    corr, warnings, entry}。quantile 只为第一个选中因子出图（UI 可切换）。

    entry 默认 t1_open：与回测引擎「收盘后看信号、次日开盘成交」的纪律一致；
    旧版 T 收盘起算会把不可交易的隔夜段计入分层净值，属系统性乐观偏差。
    """
    mats = matrices(panel)
    warnings: List[str] = []
    if len(panel.symbols) < min_cross:
        warnings.append(
            f"横截面只有 {len(panel.symbols)} 个标的（<{min_cross}），IC/分层统计意义很弱，"
            "请扩池（如沪深300成分股）后再看结论。")
    if panel.n_dates < 120:
        warnings.append("样本不足 120 个交易日，IC 波动大，结论不稳。")

    facs: Dict[str, pd.DataFrame] = {}
    labels: Dict[str, str] = {}
    for k in keys:
        facs[k] = factor_matrix(mats, k)
        labels[k] = FACTORS[k]["label"]
    if expr:
        facs["expr"] = eval_expression(expr, mats)
        labels["expr"] = f"表达式: {expr}"

    rows = []
    ic_map: Dict[str, pd.Series] = {}
    for k, f in facs.items():
        ic = _row_spearman(f, _fwd_ret(mats, 1, entry=entry), min_cross)
        ic_map[k] = ic
        s = ic_summary(ic)
        rows.append({"因子": labels[k], **s})
    summary = pd.DataFrame(rows).set_index("因子")

    first_key = keys[0] if keys else "expr"
    qnav = quantile_nav(facs[first_key], mats, nq=nq, min_cross=min_cross, entry=entry)
    hor = horizon_ic(facs[first_key], mats, horizons=horizons, min_cross=min_cross, entry=entry)
    ac = factor_autocorr(facs[first_key], min_cross=min_cross)
    if qnav.empty:
        warnings.append("有效截面样本不足，分层净值未生成（扩池或降低 min_cross 后再试）。")

    return {
        "panel_dates": (panel.dates[0], panel.dates[-1]),
        "n_symbols": len(panel.symbols),
        "focus": labels[first_key],
        "summary": summary,
        "ic": ic_map,
        "quantile_nav": qnav,
        "horizon": hor,
        "autocorr": ac,
        "corr": cross_factor_corr(facs, min_cross=min_cross),
        "warnings": warnings,
        "entry": entry,
    }
