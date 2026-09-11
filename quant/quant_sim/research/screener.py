"""条件选股器：本地 DuckDB 全市场日线粗筛（行情/技术/流动性）＋远端估值快照精筛。

口径（写死并标注，防自欺）：
* 行情取 ``v_daily_qfq``（前复权，锚定最新交易日）：末日 close ≡ 真实收盘价，
  相邻日比值 ≡ 真实涨跌幅，均线/动量/新高类条件在复权序列上无未来函数；
* 估值/名称来自远端 ``valuation snapshot``（当前时点快照，≤100 代码/次），
  只对本地条件筛出的候选富化——PE/PB 是「现在」的值，不 point-in-time，
  回看历史筛选会引入幸存者/估值前视偏差，仅供当日选股，不可直接回测；
* 新股判定用「本地库可观察交易日数」（2016 至今），近似上市时长；
* 股票池：全市场或指数成分（``index constituents`` 远端快照，当前成分有前视，
  同上不用于历史研究）；
* 停牌日缺行 = 当日不参与（与撮合层同一语义）。

缓存：行情导出 ``data/screener/market_qfq_<asof>_n<days>.parquet``（按基准日一份）；
指数成分 ``data/screener/const_<code>_<今天>.json``（当日有效）。
CLI：``python -m quant_sim.research.screener --template 趋势多头 --limit 30``
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from quant_sim.core.fsutil import save_json_atomic
from quant_sim.data.hithink import _cli

CACHE_DIR = (Path(__file__).resolve().parents[2] / "data" / "screener")

__all__ = [
    "CONDITIONS", "TEMPLATES", "UNIVERSES", "DISPLAY_COLS", "GROUPS",
    "build_factors", "get_universe", "attach_valuation", "screen", "board_of", "to_thscode",
]

# ------------------------------------------------------------------ 板块/代码工具
def board_of(ticker: str) -> str:
    t = str(ticker)[:6]
    if t.startswith(("300", "301", "302")):
        return "创业板"
    if t.startswith(("688", "689")):
        return "科创板"
    if t.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def _suffix(ticker: str) -> str:
    t = str(ticker)[:6]
    if t[:2] in ("60", "68", "50", "51", "52", "56", "58", "90"):
        return "SH"
    if t[0] in ("0", "1", "3"):
        return "SZ"
    return "BJ"


def to_thscode(ticker: str) -> str:
    return f"{str(ticker)[:6]}.{_suffix(ticker)}"


# ------------------------------------------------------------------ 行情导出与因子
_MA_NS = (5, 10, 20, 60, 120)
_MOM_NS = (5, 20, 60, 120)


def _export_market(start: str) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / f"_export_{os.getpid()}.parquet"
    sql = (
        f"select thscode, date, open, high, low, close, volume, amount "
        f"from v_daily_qfq where date >= '{start}' order by thscode, date"
    )
    try:
        _cli(["db", "export", "--sql", sql, "--file-format", "parquet", "--output", str(tmp)], timeout=600)
        df = pd.read_parquet(tmp)
    finally:
        if tmp.exists():
            os.remove(tmp)
    df["ticker"] = df["thscode"].str.slice(0, 6)
    return df


def _prune_caches(keep: Path, pattern: str = "market_qfq_*.parquet", n: int = 3) -> None:
    old = sorted(CACHE_DIR.glob(pattern))
    for p in old[:-n] if len(old) > n else []:
        try:
            if p != keep:
                os.remove(p)
        except OSError:
            pass


def build_factors(days: int = 320, refresh: bool = False) -> pd.DataFrame:
    """本地库全市场最新横截面因子表。行=6 位代码，attrs 含 asof/window_days。

    days：观察窗口（交易日）。MA120/120 日动量至少要 121，默认 320 留足余量。
    """
    from quant_sim.data.hithink import _cn_trading_dates

    all_days = sorted(str(d)[:10] for d in _cn_trading_dates())
    win = all_days[-days:]
    asof = win[-1]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"market_qfq_{asof}_n{days}.parquet"
    cached = cache.exists() and not refresh
    if not cached:
        df = _export_market(win[0])
        df.to_parquet(cache)
        _prune_caches(cache)
    else:
        df = pd.read_parquet(cache)
    factors = _compute_factors(df)
    factors.attrs["asof"] = asof
    factors.attrs["window_days"] = int(df["date"].nunique())
    factors.attrs["from_cache"] = cached
    return factors


def _pivot(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.pivot_table(index="date", columns="ticker", values=col, aggfunc="last").sort_index()


def _compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """长表 → 最新一日每只股票的因子行（宽矩阵向量化滚动）。"""
    close = _pivot(df, "close")
    high = _pivot(df, "high")
    volume = _pivot(df, "volume")
    amount = _pivot(df, "amount")
    ret1 = close.pct_change()

    out = pd.DataFrame(index=close.columns)
    rel = close.iloc[-1]
    out["close"] = rel
    out["chg_1d"] = ret1.iloc[-1]
    for n in _MA_NS:
        out[f"ma_{n}"] = close.rolling(n).mean().iloc[-1] if len(close) >= n else np.nan
    out["above_ma20"] = rel > out["ma_20"]
    out["above_ma60"] = rel > out["ma_60"]
    out["above_ma120"] = rel > out["ma_120"]
    out["ma_bull"] = (rel > out["ma_5"]) & (out["ma_5"] > out["ma_10"]) & (out["ma_10"] > out["ma_20"]) & (out["ma_20"] > out["ma_60"])
    for n in _MOM_NS:
        out[f"mom_{n}"] = rel / close.iloc[-1 - n] - 1.0 if len(close) > n else np.nan
    out["vol_ann_20"] = ret1.rolling(20).std().iloc[-1] * np.sqrt(244) if len(ret1) >= 21 else np.nan
    out["amt_20"] = amount.rolling(20).mean().iloc[-1] / 1e8 if len(amount) >= 20 else np.nan  # 亿元
    out["vol_ratio"] = volume.iloc[-1] / volume.shift(1).rolling(5).mean().iloc[-1] if len(volume) >= 6 else np.nan
    up = (ret1 > 0).astype("float64").where(ret1.notna())  # 停牌 NaN 不参与胜率均值
    out["upday_20"] = up.rolling(20).mean().iloc[-1] if len(up) >= 21 else np.nan
    if len(high) >= 60:
        hh60 = high.rolling(60).max().iloc[-1]
        out["dd_60"] = rel / hh60 - 1.0  # ≤0：离 60 日最高点的回撤深度
        out["new_high_60"] = (high.iloc[-1] >= hh60 - 1e-9).fillna(False)
    else:
        out["dd_60"] = np.nan
        out["new_high_60"] = False
    out["hist_days"] = close.notna().sum()
    out["board"] = [board_of(t) for t in out.index]
    return out


# ------------------------------------------------------------------ 股票池
UNIVERSES: Dict[str, Optional[str]] = {
    "全市场": None,
    "上证50": "000016.SH",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "中证1000": "000852.SH",
    "科创50": "000688.SH",
    "创业板指": "399006.SZ",
}


def get_universe(name: str) -> Optional[List[str]]:
    """股票池名 → 6 位代码列表；全市场返回 None（不裁剪）。成分当日缓存。"""
    code = UNIVERSES.get(name)
    if code is None:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / f"const_{code.split('.')[0]}_{pd.Timestamp.today():%Y%m%d}.json"
    if fp.exists():
        items = json.loads(fp.read_text(encoding="utf-8"))
    else:
        tmp = CACHE_DIR / f"_const_{os.getpid()}.json"
        try:
            _cli(["index", "constituents", "--thscode", code, "--output", str(tmp)], timeout=120)
            data = json.loads(tmp.read_text(encoding="utf-8"))["data"]
            items = data.get("item") if isinstance(data, dict) else data
            save_json_atomic(fp, items)
        finally:
            if tmp.exists():
                os.remove(tmp)
    return [str(r["ticker"])[:6] for r in items if r.get("ticker")]


# ------------------------------------------------------------------ 估值/名称富化
VALUATION_COLS = ["name", "pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm"]


def attach_valuation(tickers: Sequence[str], pause: float = 0.15) -> pd.DataFrame:
    """远端估值快照（≤100 代码/次）→ 以 6 位代码为索引的名称+估值表。失败批次跳过。"""
    import time

    codes = [str(t)[:6] for t in dict.fromkeys(tickers)]
    rows: List[dict] = []
    for i in range(0, len(codes), 100):
        batch = ",".join(to_thscode(c) for c in codes[i:i + 100])
        data = None
        try:
            # _cli 内部已带 4 次退避重试，外层不再叠层（曾 2×4=8 次子进程调用，失败时延迟放大）
            data = _cli(["valuation", "snapshot", "--thscodes", batch], timeout=90)
        except Exception as e:
            print(f"  [warn] 估值富化批次 {i // 100 + 1} 失败：{e}")
        if data is not None:
            rows.extend(data.get("item") or [])
        if i + 100 < len(codes):
            time.sleep(pause)
    if not rows:
        return pd.DataFrame(columns=VALUATION_COLS).rename_axis("ticker")
    df = pd.DataFrame(rows)
    df["ticker"] = df["thscode"].str.slice(0, 6)
    df = df.drop_duplicates("ticker").set_index("ticker")
    return df.reindex(columns=[c for c in VALUATION_COLS if c in df.columns])


# ------------------------------------------------------------------ 条件库
def _mask_range(d: pd.DataFrame, col: str, lo: Optional[float], hi: Optional[float]) -> pd.Series:
    if col not in d.columns:
        raise KeyError(f"缺列 {col}（估值条件需先富化）")
    x = pd.to_numeric(d[col], errors="coerce")
    m = x.notna()
    if lo is not None:
        m &= x >= lo
    if hi is not None:
        m &= x <= hi
    return m


def _rng(col: str, scale: float = 1.0):
    """range 条件工厂：lo/hi 按 UI 单位给出（% 条件 scale=100），内部换算存储值。"""
    return {
        "kind": "range", "col": col, "scale": scale,
        "apply": lambda d, lo, hi, s=scale: _mask_range(
            d, col, None if lo is None else lo / s, None if hi is None else hi / s),
    }


CONDITIONS: Dict[str, dict] = {
    # ---- 行情与流动性（本地，NaN 视为不满足）
    "close":     {**_rng("close"), "label": "收盘价(元)", "group": "行情与流动性", "unit": "元", "step": 1.0, "dec": 2},
    "chg_1d":    {**_rng("chg_1d", 100), "label": "当日涨幅(%)", "group": "行情与流动性", "unit": "%", "step": 1.0, "dec": 1},
    "amt_20":    {**_rng("amt_20"), "label": "20日均成交额(亿)", "group": "行情与流动性", "unit": "亿", "step": 0.5, "dec": 1},
    "hist_days": {**_rng("hist_days"), "label": "可观察交易日数(≥120 剔次新)", "group": "行情与流动性", "unit": "天", "step": 10.0, "dec": 0},
    # ---- 趋势与动量
    "mom_5":   {**_rng("mom_5", 100), "label": "5日涨幅(%)", "group": "趋势与动量", "unit": "%", "step": 1.0, "dec": 1},
    "mom_20":  {**_rng("mom_20", 100), "label": "20日涨幅(%)", "group": "趋势与动量", "unit": "%", "step": 1.0, "dec": 1},
    "mom_60":  {**_rng("mom_60", 100), "label": "60日涨幅(%)", "group": "趋势与动量", "unit": "%", "step": 1.0, "dec": 1},
    "mom_120": {**_rng("mom_120", 100), "label": "120日涨幅(%)", "group": "趋势与动量", "unit": "%", "step": 1.0, "dec": 1},
    "dd_60":   {**_rng("dd_60", 100), "label": "距60日高点(%)（-20=回撤两成）", "group": "趋势与动量", "unit": "%", "step": 1.0, "dec": 1},
    "above_ma20":  {"kind": "bool", "label": "收盘 > MA20", "group": "趋势与动量",
                    "apply": lambda d: d["above_ma20"].fillna(False).astype(bool)},
    "above_ma60":  {"kind": "bool", "label": "收盘 > MA60", "group": "趋势与动量",
                    "apply": lambda d: d["above_ma60"].fillna(False).astype(bool)},
    "above_ma120": {"kind": "bool", "label": "收盘 > MA120", "group": "趋势与动量",
                    "apply": lambda d: d["above_ma120"].fillna(False).astype(bool)},
    "ma_bull":  {"kind": "bool", "label": "均线多头排列（收>MA5>MA10>MA20>MA60）", "group": "趋势与动量",
                 "apply": lambda d: d["ma_bull"].fillna(False).astype(bool)},
    "new_high_60": {"kind": "bool", "label": "今日创60日新高", "group": "趋势与动量",
                    "apply": lambda d: d["new_high_60"].fillna(False).astype(bool)},
    # ---- 强弱与量能
    "vol_ratio":  {**_rng("vol_ratio"), "label": "量比（今量/前5日均量）", "group": "强弱与量能", "unit": "倍", "step": 0.1, "dec": 2},
    "vol_ann_20": {**_rng("vol_ann_20", 100), "label": "20日年化波动率(%)", "group": "强弱与量能", "unit": "%", "step": 5.0, "dec": 0},
    "upday_20":   {**_rng("upday_20", 100), "label": "20日上涨天数占比(%)", "group": "强弱与量能", "unit": "%", "step": 5.0, "dec": 0},
    # ---- 估值（远端快照，精筛层）
    "pe_ttm":  {**_rng("pe_ttm"), "label": "市盈率PE(TTM)", "group": "估值精筛(远端快照)", "unit": "倍", "step": 5.0, "dec": 1, "valuation": True},
    "pb_mrq":  {**_rng("pb_mrq"), "label": "市净率PB(最新)", "group": "估值精筛(远端快照)", "unit": "倍", "step": 0.5, "dec": 2, "valuation": True},
    "ps_ttm":  {**_rng("ps_ttm"), "label": "市销率PS(TTM)", "group": "估值精筛(远端快照)", "unit": "倍", "step": 1.0, "dec": 2, "valuation": True},
    "pcf_ttm": {**_rng("pcf_ttm"), "label": "市现率PCF(TTM)", "group": "估值精筛(远端快照)", "unit": "倍", "step": 5.0, "dec": 1, "valuation": True},
    "exclude_st": {
        "kind": "bool", "valuation": True, "label": "排除 ST/*ST/退市（按名称）", "group": "估值精筛(远端快照)",
        "apply": lambda d: ~d["name"].astype(str).str.contains("ST|退", na=False) if "name" in d.columns else pd.Series(True, index=d.index),
    },
}

GROUPS = ["行情与流动性", "趋势与动量", "强弱与量能", "估值精筛(远端快照)"]

#: 常用配方：bools + ranges {key: (lo, hi)}（与 UI 同单位；None=不限）
TEMPLATES: Dict[str, dict] = {
    "趋势多头": {"bools": ["ma_bull"], "ranges": {"mom_20": (5, None), "vol_ratio": (1.0, None), "amt_20": (1.0, None)},
                 "note": "均线多头 + 20日动量>5% + 量比>1 + 日均成交额>1亿"},
    "超跌放量反弹": {"bools": [], "ranges": {"dd_60": (None, -15), "chg_1d": (0, None), "vol_ratio": (1.5, None), "amt_20": (1.0, None)},
                 "note": "距60日高点回撤超15% + 今日收红 + 量比>1.5（左侧抄底，注意情绪）"},
    "强势创新高": {"bools": ["new_high_60"], "ranges": {"mom_60": (10, None), "amt_20": (1.0, None)},
               "note": "创60日新高 + 60日动量>10%：强者恒强，回撤风险自承担"},
    "低估值蓝筹": {"bools": ["above_ma120", "exclude_st"], "ranges": {"pe_ttm": (0.1, 15), "pb_mrq": (0.1, 2.0), "amt_20": (2.0, None)},
               "note": "PE<15 且 PB<2 且日均成交额>2亿，年线上方、非ST（估值为当前快照）"},
    "小而美动量": {"bools": ["above_ma20"], "ranges": {"mom_20": (10, None), "amt_20": (0.5, 3.0), "vol_ann_20": (None, 60)},
               "note": "中小成交池里的中期动量，年化波动上限 60% 控妖股"},
}

#: 结果表展示列（存在才显示）与 % 换算列（存储 0.12 → 显示 12.0）
DISPLAY_COLS = {
    "ticker": "代码", "name": "名称", "board": "板块", "close": "收盘", "chg_1d": "当日%",
    "mom_5": "5日%", "mom_20": "20日%", "mom_60": "60日%", "dd_60": "距60日高%",
    "vol_ratio": "量比", "amt_20": "日均额(亿)", "vol_ann_20": "波动率%(年化)", "upday_20": "20日胜率%",
    "ma_bull": "多头", "new_high_60": "新高", "hist_days": "可观察天数",
    "pe_ttm": "PE(TTM)", "pb_mrq": "PB", "ps_ttm": "PS", "pcf_ttm": "PCF",
}
_PCT_KEYS = {"chg_1d", "mom_5", "mom_20", "mom_60", "mom_120", "dd_60", "vol_ann_20", "upday_20"}


def _fmt_pct(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    for c in _PCT_KEYS & set(out.columns):
        out[c] = pd.to_numeric(out[c], errors="coerce") * 100
    return out


# ------------------------------------------------------------------ 主流程
def screen(
    factors: pd.DataFrame,
    universe: Optional[Sequence[str]] = None,
    boards: Optional[Sequence[str]] = None,
    bools: Optional[Sequence[str]] = None,
    ranges: Optional[Dict[str, tuple]] = None,
    sort_by: str = "amt_20",
    ascending: bool = False,
    limit: int = 200,
    enrich: bool = True,
    max_enrich: int = 600,
    progress=None,
) -> dict:
    """两段式筛选：本地因子条件 →（可选/需要时）估值富化 → 估值/名称条件 → 排序截断。

    返回 {table, steps:{pool, local, enriched, final}, warnings, asof}；
    table 含 ticker 列，比例列已换算成百分数，可直接展示/下载。
    """
    warnings: List[str] = []
    ranges = {k: v for k, v in (ranges or {}).items() if v and (v[0] is not None or v[1] is not None)}
    bools = list(bools or [])
    unknown = [k for k in bools + list(ranges) if k not in CONDITIONS]
    if unknown:
        raise KeyError(f"未知条件：{unknown}")
    d = factors
    if universe is not None:
        uni = {str(c)[:6] for c in universe}
        d = d[d.index.isin(uni)]
        if d.index.size < max(len(uni) * 0.6, 1):
            warnings.append(f"股票池 {len(uni)} 只中仅 {d.index.size} 只有本地行情（退市/未覆盖？）")
    if boards:
        d = d[d["board"].isin(boards)]
    n_pool = d.index.size
    if n_pool == 0:
        return {"table": d.reset_index().rename(columns={"index": "ticker"}),
                "steps": {"pool": 0, "local": 0, "enriched": 0, "final": 0},
                "warnings": warnings + ["股票池为空"], "asof": factors.attrs.get("asof")}

    val_keys = [k for k in bools + list(ranges) if CONDITIONS[k].get("valuation")]
    do_enrich = bool(enrich or val_keys)
    # 第一段：本地条件（bool + 非估值 range）
    for k in [x for x in bools if not CONDITIONS[x].get("valuation")]:
        m = CONDITIONS[k]["apply"](d)
        d = d[m.reindex(d.index).fillna(False)]
    for k, (lo, hi) in ranges.items():
        if CONDITIONS[k].get("valuation"):
            continue
        m = CONDITIONS[k]["apply"](d, lo, hi)
        d = d[m.reindex(d.index).fillna(False)]
    n_local = d.index.size

    # 第二段：估值富化 + 估值/名称条件
    enriched = 0
    if do_enrich:
        if d.index.size > max_enrich:
            _key = sort_by if sort_by in d.columns else "amt_20"
            d = d.sort_values(_key, ascending=not ascending, na_position="last").head(max_enrich)
            warnings.append(f"本地筛后仍超富化上限：只取排序前 {max_enrich} 名拉估值/名称——估值类条件不完整，请先收紧行情条件")
        if progress:
            progress(f"估值/名称富化 {d.index.size} 只（约 {-(-d.index.size // 100)} 次远端请求）…")
        v = attach_valuation(d.index)
        enriched = int(len(v))
        if enriched < d.index.size:
            warnings.append(f"估值快照仅命中 {enriched}/{d.index.size} 只（新股/停牌/接口缺数），未命中者估值条件按不满足处理")
        d = d.join(v, how="left")
        if "name" in d.columns:
            d["name"] = d["name"].fillna("")
        for k in [x for x in bools if CONDITIONS[x].get("valuation")]:
            m = CONDITIONS[k]["apply"](d)
            d = d[m.reindex(d.index).fillna(False)]
        for k, (lo, hi) in ranges.items():
            if not CONDITIONS[k].get("valuation"):
                continue
            if k not in d.columns:
                warnings.append(f"估值列 {k} 缺失，该条件未生效")
                continue
            m = CONDITIONS[k]["apply"](d, lo, hi)
            d = d[m.reindex(d.index).fillna(False)]
    n_final = d.index.size

    if sort_by not in d.columns:
        warnings.append(f"排序列 {sort_by} 不存在，改用 20 日均成交额")
        sort_by = "amt_20" if "amt_20" in d.columns else d.columns[0]
    d = d.sort_values(sort_by, ascending=ascending, na_position="last")
    if limit and limit > 0:
        d = d.head(limit)
    table = _fmt_pct(d.reset_index().rename(columns={"index": "ticker"}))
    return {
        "table": table,
        "steps": {"pool": n_pool, "local": n_local, "enriched": enriched, "final": n_final},
        "warnings": warnings,
        "asof": factors.attrs.get("asof"),
    }


# ------------------------------------------------------------------ CLI
def _main() -> None:
    ap = argparse.ArgumentParser(description="条件选股（本地行情粗筛 + 远端估值精筛）")
    ap.add_argument("--template", default=None, choices=list(TEMPLATES), help="内置配方")
    ap.add_argument("--universe", default="全市场", choices=list(UNIVERSES))
    ap.add_argument("--boards", default=None, help="限定板块，逗号分隔：主板,创业板,科创板,北交所")
    ap.add_argument("--sort", default="amt_20")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=320)
    ap.add_argument("--no-enrich", action="store_true", help="跳过远端估值富化（更快）")
    ap.add_argument("--refresh", action="store_true", help="忽略行情缓存重新导出")
    ap.add_argument("--out", default=None, help="结果 CSV 落盘路径")
    args = ap.parse_args()

    f = build_factors(days=args.days, refresh=args.refresh)
    print(f"因子基准日 {f.attrs['asof']}｜{f.index.size} 只｜窗口 {f.attrs['window_days']} 交易日"
          f"{'（缓存）' if f.attrs['from_cache'] else ''}")
    tpl = TEMPLATES.get(args.template or "", {})
    boards = [b.strip() for b in args.boards.split(",")] if args.boards else None
    res = screen(f, universe=get_universe(args.universe), boards=boards, bools=tpl.get("bools"),
                 ranges=tpl.get("ranges"), sort_by=args.sort, limit=args.limit, enrich=not args.no_enrich)
    for w in res["warnings"]:
        print("⚠", w)
    s = res["steps"]
    print(f"漏斗：池 {s['pool']} → 本地筛 {s['local']} → 富化 {s['enriched']} → 终选 {s['final']}")
    show = [c for c in ["ticker", "name", "board", "close", "chg_1d", "mom_20", "vol_ratio", "amt_20", "pe_ttm", "pb_mrq"]
            if c in res["table"].columns]
    print(res["table"][show].to_string(index=False))
    if args.out:
        res["table"].to_csv(args.out, index=False, encoding="utf-8-sig")
        print("→", args.out)


if __name__ == "__main__":
    _main()
