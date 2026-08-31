"""hithink-finance 本地 DuckDB → 量化平台数据契约桥接。

数据源：`hithink-finance` CLI 管理的本地 market.duckdb（A 股个股日线，
2016 至今，含 qfq/hfq 连续复权视图）。同步/初始化由 `hithink-finance data sync/init`
负责（本模块只读，不写库）。

映射到平台契约（docs/design.md §5.1）：
  * 视图选择：qfq（默认，展示友好）/ hfq / raw
  * symbol 用 6 位裸代码（A 股内唯一）；pre_close 用连续复权序列的 shift(1)
    —— qfq/hfq 序列相邻两日之比恒等于真实涨跌幅，除权日也成立，
       因此 ±10%/20% 涨跌停判定与除权参考价一致，无需因子表
  * 停牌 = 当日整行缺失（库里 volume=0 行为 0 条，已验证）
  * raw 视图自带 prev_close 但有空洞，桥接层统一用 shift 补齐

已知限制：
  * 本地库无 ETF/LOF/指数（asset_type 仅 a-share）——ETF 需另行接入（akshare 或
    `hithink-finance market history` 远端接口）
  * 无 ST 标记（v_symbol.name 为空）——ST 股涨跌停按 10% 处理，偏乐观；
    需要时请人工传 Contract.limit_overrides 或维护黑名单

CLI：
  python -m quant_sim.data.hithink status
  python -m quant_sim.data.hithink export --symbols 600519 000001 300750 --start 2020-01-01
  python -m quant_sim.data.hithink export --file universe.txt --out data/cn_a/daily
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Sequence

import pandas as pd

_VIEW = {"qfq": "v_daily_qfq", "hfq": "v_daily_hfq", "raw": "v_daily"}

#: 裸代码即可识别的常用指数基准（其他指数请带后缀，如 000001.SH 上证指数；
#: 裸 000001 仍路由到平安银行个股）。
KNOWN_INDEX_CODES = {
    "000300",  # 沪深300
    "000905",  # 中证500
    "000852",  # 中证1000
    "000016",  # 上证50
    "000688",  # 科创50
    "399006",  # 创业板指
    "399001",  # 国证2000（深）
    "399330",  # 沪深300（深）
    "899050",  # 北证50
}


def _is_index_code(raw: str) -> bool:
    raw = str(raw)
    code = raw.split(".")[0]
    if "." in raw:
        suffix = raw.split(".")[1].upper()
        return (
            code in KNOWN_INDEX_CODES
            or (suffix == "SH" and code.startswith(("000", "899", "931", "932")))
            or (suffix == "SZ" and code.startswith("399"))
        )
    return code in KNOWN_INDEX_CODES


def _cli(args: Sequence[str], timeout: int = 300) -> dict:
    exe = shutil.which("hithink-finance") or shutil.which("hithink-finance.cmd")
    if exe is None:
        raise RuntimeError("未找到 hithink-finance CLI，请先安装（见 hithink-finance-shared skill）")
    proc = subprocess.run([exe, *args, "--format", "json"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    try:
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"hithink-finance 输出解析失败:\n{(proc.stdout or '')[:500]}\n{(proc.stderr or '')[:500]}")
    if not payload.get("ok"):
        err = payload.get("error", {})
        raise RuntimeError(f"hithink-finance {err.get('code')}: {err.get('message')}")
    return payload["data"]


# ------------------------------------------------------------------ 状态
def _clamp_5y_window(start: pd.Timestamp, label: str) -> pd.Timestamp:
    """hithink 远端 history 接口实际为**滚动 5 年窗口**（capabilities 标的
    five-years/ten-years 均不完全保线）：start 早于今日-5y 会静默返回空。
    这里钉到窗口内并告警；更早历史请用本地库（个股 v_daily 有 2016 至今）。"""
    st = pd.Timestamp(start).normalize()
    floor = pd.Timestamp.today().normalize() - pd.DateOffset(years=5) + pd.Timedelta(days=15)
    if st < floor:
        print(f"[warn] {label}: 远端接口为滚动 5 年窗口，起始日 {st.date()} 钉到 {floor.date()}（更早历史请用本地库/重新 sync）")
        return floor
    return st


def db_status() -> dict:
    return _cli(["data", "status"])


def coverage() -> dict:
    row = _cli(["db", "query", "--sql", "select min(date) d0, max(date) d1, count(distinct thscode) symbols, count(*) rows from v_daily"])[0]
    return {k: v for k, v in row.items()}


# ------------------------------------------------------------------ 导出
def _quote(value: str) -> str:
    return str(value).replace("'", "''")


def export_daily(
    symbols: Sequence[str],
    start: str = "2018-01-01",
    end: Optional[str] = None,
    adjust: str = "qfq",
    out_dir: str = "data/cn_a/daily",
) -> Dict[str, str]:
    """把股票池日线导出为平台契约格式（每标的一个 Parquet）。返回 {symbol: path}。"""
    if adjust not in _VIEW:
        raise ValueError(f"adjust 只支持 {list(_VIEW)}")
    if not symbols:
        raise ValueError("symbols 为空")
    view = _VIEW[adjust]
    codes = [str(s).split(".")[0] for s in symbols]
    code_list = ",".join(f"'{_quote(c)}'" for c in dict.fromkeys(codes))
    end_sql = f"and date <= '{_quote(end)}'" if end else ""
    sql = (
        f"select thscode, date, open, high, low, close, volume, amount "
        f"from {view} where substr(thscode,1,6) in ({code_list}) "
        f"and date >= '{_quote(start)}' {end_sql} order by thscode, date"
    )
    os.makedirs(out_dir, exist_ok=True)
    from .manifest import entry_for, update_manifest

    _man_entries: Dict[str, dict] = {}
    tmp = os.path.join(out_dir, "_hithink_export.parquet")
    result = _cli(["db", "export", "--sql", sql, "--file-format", "parquet", "--output", tmp], timeout=600)
    df = pd.read_parquet(tmp)
    os.remove(tmp)
    if df.empty:
        raise RuntimeError(f"导出 0 行：检查代码是否正确/日期区间是否在库范围内（{coverage()}）")
    df["ticker"] = df["thscode"].str.slice(0, 6)
    written: Dict[str, str] = {}
    for ticker, g in df.groupby("ticker"):
        g = g.drop_duplicates(subset="date", keep="last").sort_values("date")
        g = g.rename(columns={"volume": "volume", "amount": "amount"})
        g["pre_close"] = g["close"].shift(1)
        g["is_st"] = False
        frame = g.set_index(pd.to_datetime(g["date"]).dt.normalize())[
            ["open", "high", "low", "close", "volume", "amount", "pre_close", "is_st"]
        ].astype({"is_st": "bool"})
        path = os.path.join(out_dir, f"{ticker}.parquet")
        frame.to_parquet(path)
        written[ticker] = path
        _man_entries[f"{ticker}.parquet"] = entry_for(
            frame, adjust=adjust, source=f"hithink-local:{view}",
            pre_close="上一收盘推算（非交易所除权参考价，除权日依赖复权序列正确）",
        )
    missing = set(codes) - set(written)
    if missing:
        print(f"[warn] 库中无数据（代码不存在/区间无行情）: {sorted(missing)}")
    update_manifest(out_dir, _man_entries)
    return written


# ------------------------------------------------------------------ ETF（远端 fund.history）

def _cn_trading_dates() -> set:
    """以本地个股库的日期集为 A 股交易日历（已调通库 ≡ 真实开市日）。

    背景：hithink fund.history 会返回寒暑假补班日等非交易日脏数据（如 2024-10-07
    假 +20%），必须用交易日历过滤。
    """
    cached = getattr(_cn_trading_dates, "_cache", None)
    if cached is not None:
        return cached
    rows = _cli(["db", "query", "--sql", "select distinct date from v_daily"], timeout=120)
    days = {str(r["date"])[:10] for r in rows}
    if len(days) < 500:
        raise RuntimeError(f"本地库交易日历异常（仅 {len(days)} 天），先跑 hithink-finance data status")
    _cn_trading_dates._cache = days
    return days

def _exchange_suffix(ticker: str) -> str:
    # 与 Contract.is_fund/FUND_PREFIXES 保持同步：场内基金 5 字头在沪、1 字头在深。
    return "SH" if ticker[:2] in ("50", "51", "52", "53", "56", "58", "60") else "SZ"


#: 最近一次导出的疑似错价统计 {code: 条数}（含 keep 模式的仅标记），供 UI 回显
OUTLIER_STATS: Dict[str, int] = {}


def _fund_dividends(thscode: str) -> List[dict]:
    """拉取场内基金分红记录（失败返回空表，不阻断主流程）。"""
    try:
        data = _cli(["fund", "dividends", "--fund-type", "exchange", "--thscode", thscode], timeout=60)
        items = data.get("item") or []
        return [r for r in items if r.get("ex_dividend_date_ms") and (r.get("per_ten_cash_before_tax") or 0) > 0]
    except Exception as e:
        print(f"  [warn] {thscode} 分红记录拉取失败，本只按未复权处理：{e}")
        return []


def apply_fund_dividend_adjust(frame: pd.DataFrame, dividends: List[dict]) -> pd.DataFrame:
    """用分红记录把未复权市价修正为前复权连续价（返回副本）。

    口径：每份分红 = per_ten_cash_before_tax / 10；除息参考价 = 前收 - 每份分红，
    除息日之前所有 OHLC 乘系数（参考价/前收）；末尾重建 pre_close 链。

    ⚠️ 厂商口径实测结论（重要）：hithink fund.history 统一返回含派息再投资的复权
    序列（510880 两个 ~4.6% 应然缺口日无坑；510500 四个除息日三个无坑）。若叠加
    复权会凭空造出假涨幅，故用除息日投票探测：只统计应然缺口 ≥0.8% 的日子（坑太浅
    噪声盖不住信号），实测接近零 → 复权票；接近应然缺口 → 未复权票；模糊票就近。
    未复权票 > 复权票才补复权；否则原样返回并置 attrs['already_adjusted']。
    同一日多笔分红累加；窗口外/首日除息不参与投票。
    """
    if not dividends or len(frame) == 0:
        return frame
    raw = frame  # 原始市价，用于算每笔除息的局部参考价
    div_by_day: Dict[pd.Timestamp, float] = {}
    for d in dividends:
        ex = pd.to_datetime(d["ex_dividend_date_ms"] + 8 * 3600 * 1000, unit="ms").normalize()
        div_by_day[ex] = div_by_day.get(ex, 0.0) + float(d["per_ten_cash_before_tax"]) / 10.0
    # --- 口径探测：除息日投票 ---
    adj_votes = raw_votes = probe_days = 0
    for ex, dps in sorted(div_by_day.items()):
        if ex not in frame.index:
            continue
        i = frame.index.get_loc(ex)
        if i == 0:
            continue
        prev_close = float(frame["close"].iloc[i - 1])
        expect_gap = -dps / prev_close
        if abs(expect_gap) < 0.008:
            continue
        probe_days += 1
        chain = float(frame["close"].iloc[i]) / prev_close - 1
        if abs(chain) <= 0.45 * abs(expect_gap):
            adj_votes += 1  # 坑被厂商填了 → 已复权
        elif abs(chain - expect_gap) <= 0.45 * abs(expect_gap):
            raw_votes += 1  # 坑还在 → 未复权
        elif abs(chain - expect_gap) < abs(chain):
            raw_votes += 1
        else:
            adj_votes += 1
    out = frame.copy()
    if raw_votes <= adj_votes:
        meta = dict(out.attrs or {})
        meta["already_adjusted"] = True
        meta["probe_days"] = probe_days
        out.attrs = meta
        return out
    n_applied = 0
    for ex, dps in sorted(div_by_day.items()):
        if ex not in out.index:
            continue
        i = out.index.get_loc(ex)
        if i == 0:
            continue  # 序列首日除息：无前收可定系数
        prev_close = float(raw["close"].iloc[i - 1])
        ref = prev_close - dps
        if not (0.0 < ref < prev_close):
            print(f"  [warn] {ex.date()} 除息 {dps:.4f}/份 超出前收 {prev_close:.4f}，跳过该笔复权")
            continue
        factor = ref / prev_close
        cols = ["open", "high", "low", "close"]
        out.loc[out.index[:i], cols] = out.loc[out.index[:i], cols] * factor  # 除息日之前全部前移
        n_applied += 1
    if n_applied:
        out["pre_close"] = out["close"].shift(1)  # 统一重建前收链（含除息日自身）
        meta = dict(out.attrs or {})
        meta["dividend_adjusted"] = n_applied
        out.attrs = meta
    return out


def export_etf_daily(
    symbols: Sequence[str],
    start: str = "2021-01-01",
    end: Optional[str] = None,
    out_dir: str = "data/cn_a/daily",
    pause: float = 0.6,
    adjust_dividends: bool = True,
    outlier_mode: str = "keep",
) -> Dict[str, str]:
    """逐只 ETF 走远端 `fund history`（≤5 年窗口自动分片）导出平台契约格式。

    adjust_dividends=True：ETF 分红口径处理（默认开）。实测 hithink fund.history
    序列已含派息再投资复权（510880 除息日无坑验证），本参数控制是否跑「除息日
    探测→必要时才复权」；关则原样使用。
    outlier_mode：疑似错价（单日涨跌幅超限制+容差）处理：keep 保留（默认）/
    drop 删行 / interpolate 几何插值（价格）+量额置 0（撮合层视同停牌）。
    ⚠️ drop 会造成跳日；interpolate 仅适合单日孤点错价。
    """
    import time

    os.makedirs(out_dir, exist_ok=True)
    from .manifest import entry_for, update_manifest

    _man_entries: Dict[str, dict] = {}
    t0 = _clamp_5y_window(pd.Timestamp(start), "fund.history")
    t1 = pd.Timestamp(end).normalize() if end else pd.Timestamp.today().normalize()
    written: Dict[str, str] = {}
    for raw in symbols:
        code = str(raw).split(".")[0]
        from ..core.contract import Contract as _C

        if not _C.is_fund(code):  # 前缀单一来源：Contract.FUND_PREFIXES，不得手抄元组
            print(f"[warn] {code} 不像场内基金代码，跳过")
            continue
        thscode = f"{code}.{_exchange_suffix(code)}"
        rows = []
        seg_start = t0
        err: Optional[Exception] = None
        while seg_start <= t1:
            seg_end = min(seg_start + pd.Timedelta(days=365 * 4), t1)
            ms0 = int(seg_start.timestamp() * 1000)
            ms1 = int(seg_end.timestamp() * 1000)
            try:
                data = _cli(["fund", "history", "--thscode", thscode, "--start-ms", str(ms0),
                             "--end-ms", str(ms1)], timeout=120)
                rows.extend(data.get("item", []))
                err = None
            except Exception as e:
                err = e
                break
            seg_start = seg_end + pd.Timedelta(days=1)
            time.sleep(pause)
        if err is not None:
            print(f"[warn] {code} 拉取失败：{err}")
            continue
        if not rows:
            print(f"[warn] {code} 区间内无数据")
            continue
        df = pd.DataFrame(rows)
        # date_ms 是北京时间零点的 epoch（UTC+8），直接按 ms 转会得到 UTC 日期，整体错一天！
        df["date"] = pd.to_datetime(df["date_ms"] + 8 * 3600 * 1000, unit="ms").dt.tz_localize("UTC").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
        # 交易日历过滤：剔除 fund.history 返回的非开市日脏数据（补班日等）
        calendar = _cn_trading_dates()
        date_strs = df["date"].dt.strftime("%Y-%m-%d")
        dropped = int((~date_strs.isin(calendar)).sum())
        if dropped:
            print(f"  {code}: 剔除 {dropped} 个非交易日行")
        df = df[date_strs.isin(calendar)]
        df = df.rename(columns={"open_price": "open", "high_price": "high", "low_price": "low",
                                "close_price": "close", "volume": "volume", "turnover": "amount"})
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
        frame = df.set_index("date")[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        frame["pre_close"] = frame["close"].shift(1)
        frame["is_st"] = False
        # 现金分红复权（默认开，带厂商口径探测）：消除除息假下跌或确认已复权
        n_div = 0
        already = False
        if adjust_dividends:
            divs = _fund_dividends(thscode)
            if divs:
                frame = apply_fund_dividend_adjust(frame, divs)
                n_div = int(frame.attrs.get("dividend_adjusted", 0))
                already = bool(frame.attrs.get("already_adjusted"))
                if n_div:
                    print(f"  {code}: 探测到未复权，已现金分红复权 {n_div} 笔")
                elif already:
                    print(f"  {code}: 除息日探测（{frame.attrs.get('probe_days', 0)} 天）→ 厂商序列已含派息复权，不重复处理")
        # 数据质量审计：单日涨跌幅超出该品种限制+容差（厂商偶发错价），按 outlier_mode 处理
        from ..core.contract import Contract

        ct = Contract()
        lim = ct.limit_pct(code, False) + 0.006
        pct = frame["close"] / frame["pre_close"] - 1
        bad_idx = list(frame.index[pct.abs() > lim])
        OUTLIER_STATS[code] = len(bad_idx)
        if bad_idx:
            if outlier_mode == "drop":
                frame = frame.drop(index=bad_idx)
                frame["pre_close"] = frame["close"].shift(1)
                print(f"  {code}: 剔除 {len(bad_idx)} 个疑似错价日 {bad_idx[0].date()} 等（drop 模式）")
            elif outlier_mode == "interpolate":
                f2 = frame.copy()
                for d in bad_idx:
                    i = f2.index.get_loc(d)
                    if i == 0 or i == len(f2) - 1:
                        f2 = f2.drop(index=d)
                        continue
                    pc, nc = float(f2["close"].iloc[i - 1]), float(f2["close"].iloc[i + 1])
                    r = (nc / pc) ** 0.5
                    mid = pc * r  # 几何中点：OHLC 同价，消除尖刺且链式收益连续
                    f2.loc[d, ["open", "high", "low", "close"]] = mid
                    f2.loc[d, ["volume", "amount"]] = 0.0
                    print(f"  {code}: {d.date()} 疑似错价已几何插值（量额置 0 并标停牌）")
                frame = f2
                frame["pre_close"] = frame["close"].shift(1)
            else:
                for d in bad_idx:
                    p = float(frame["close"].loc[d]) / float(frame["pre_close"].loc[d]) - 1
                    print(f"  [warn] {code} {pd.Timestamp(d).date()} 涨跌幅 {p:+.1%} 超出 ±{lim:.1%} 容差（疑似厂商错价，已保留原始数据）")
        path = os.path.join(out_dir, f"{code}.parquet")
        frame.to_parquet(path)
        written[code] = path
        note = f"{n_div}笔分红复权" if n_div else ("已含派息复权" if already else ("未复权" if not adjust_dividends else "无分红记录"))
        _man_entries[f"{code}.parquet"] = entry_for(
            frame,
            # 只要走过除息日探测管道（adjust_dividends=True），口径即为「含派息再投资」：
            # 探测结论是「序列本就含息」或「无分红记录」时 raw==复权序列，标 raw 会被
            # 股票口径校验误报混用。仅 --no-div-adjust（未探测）才记 raw。
            adjust=("dividend_reinvested" if adjust_dividends else "raw"),
            source="hithink:fund.history",
            outlier_mode=outlier_mode,
            outlier_days=int(OUTLIER_STATS.get(code, 0)),
            note=note,
        )
        print(f"  {code} → {path}（{len(frame)} 行，{note}）")
        time.sleep(pause)
    update_manifest(out_dir, _man_entries)
    return written


# ------------------------------------------------------------------ 指数（远端 index.history）

def export_index_daily(
    symbols: Sequence[str],
    start: str = "2018-01-01",
    end: Optional[str] = None,
    out_dir: str = "data/cn_a/daily",
    pause: float = 0.6,
) -> Dict[str, str]:
    """指数日线→平台契约（仅作基准曲线，指数不可交易；10 年窗口自动分片）。"""
    import time

    os.makedirs(out_dir, exist_ok=True)
    t0 = _clamp_5y_window(pd.Timestamp(start), "index.history")
    t1 = pd.Timestamp(end).normalize() if end else pd.Timestamp.today().normalize()
    written: Dict[str, str] = {}
    for raw in symbols:
        code = str(raw).split(".")[0]
        if "." in str(raw):
            thscode = str(raw)
        else:
            thscode = f"{code}.{'SZ' if code.startswith('399') else ('BJ' if code.startswith('899') else 'SH')}"
        rows = []
        seg = t0
        err: Optional[Exception] = None
        while seg <= t1:
            seg_end = min(seg + pd.Timedelta(days=365 * 4), t1)
            try:
                data = _cli(["index", "history", "--thscode", thscode,
                             "--start-ms", str(int(seg.timestamp() * 1000)),
                             "--end-ms", str(int(seg_end.timestamp() * 1000))], timeout=120)
                rows.extend(data.get("item", []))
                err = None
            except Exception as e:
                err = e
                break
            seg = seg_end + pd.Timedelta(days=1)
            time.sleep(pause)
        if err is not None:
            print(f"[warn] 指数 {code} 拉取失败：{err}")
            continue
        if not rows:
            print(f"[warn] 指数 {code} 区间内无数据")
            continue
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date_ms"] + 8 * 3600 * 1000, unit="ms").dt.tz_localize("UTC").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
        df = df.rename(columns={"open_price": "open", "high_price": "high", "low_price": "low",
                                "close_price": "close", "volume": "volume", "turnover": "amount"})
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
        frame = df.set_index("date")[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        frame["pre_close"] = frame["close"].shift(1)
        frame["is_st"] = False
        path = os.path.join(out_dir, f"{code}.parquet")
        frame.to_parquet(path)
        written[code] = path
        from .manifest import entry_for as _entry, update_manifest as _upd

        _upd(out_dir, {f"{code}.parquet": _entry(frame, adjust="none", source="hithink:index.history",
                                                note="指数仅作基准曲线，不可交易；不复权")})
        print(f"  指数 {code} → {path}（{len(frame)} 行）")
        time.sleep(pause)
    return written


# ------------------------------------------------------------------ 统一入口
def export_any(
    symbols: Sequence[str],
    start: str = "2018-01-01",
    end: Optional[str] = None,
    adjust: str = "qfq",
    out_dir: str = "data/cn_a/daily",
    adjust_dividends: bool = True,
    outlier_mode: str = "keep",
) -> Dict[str, str]:
    """自动分流：指数→index.history，场内基金→fund.history（分红复权默认开），个股→本地 DuckDB。"""
    idx = [c for c in symbols if _is_index_code(c)]
    rest = [c for c in symbols if c not in idx]
    from ..core.contract import Contract as _C

    # 修复前缀漂移：旧元组缺 53 段，导致 530xxx 场内基金被误路由到个股本地库后静默缺失。
    funds = [c for c in rest if _C.is_fund(str(c))]
    stocks = [str(c).split(".")[0] for c in rest if c not in funds]
    written: Dict[str, str] = {}
    if idx:
        print(f"指数 {idx} →远端 index.history：")
        written.update(export_index_daily(idx, start=start, end=end, out_dir=out_dir))
    if funds:
        print(f"ETF/LOF {len(funds)} 只→远端 fund.history：")
        written.update(export_etf_daily(funds, start=start, end=end, out_dir=out_dir,
                                        adjust_dividends=adjust_dividends, outlier_mode=outlier_mode))
    if stocks:
        written.update(export_daily(stocks, start=start, end=end, adjust=adjust, out_dir=out_dir))
    return written


def load_panel(symbols: Sequence[str], start: str = "2018-01-01", end: Optional[str] = None,
               adjust: str = "qfq", cache_dir: str = "data/cn_a/daily", auto: bool = False,
               adjust_dividends: bool = True, outlier_mode: str = "keep"):
    """导出 + 直接返回 BarPanel（Streamlit/研究脚本用）。auto=True 时自动分流指数/ETF/个股。"""
    from .loader import load_panel as _load

    codes = [str(s).split(".")[0] for s in symbols]
    if auto:
        export_any(list(symbols), start=start, end=end, adjust=adjust, out_dir=cache_dir,
                   adjust_dividends=adjust_dividends, outlier_mode=outlier_mode)
    else:
        export_daily(codes, start=start, end=end, adjust=adjust, out_dir=cache_dir)
    panel = _load(cache_dir, symbols=codes, date_range=(start, end) if end else None,
                  expect_adjust=adjust)
    panel.metadata["source"] = f"hithink:{'auto' if auto else adjust}"
    _os = {k: v for k, v in OUTLIER_STATS.items() if k in codes and v}
    if _os:
        panel.metadata["outlier_stats"] = _os
    return panel


# ------------------------------------------------------------------ CLI
def main() -> None:
    parser = argparse.ArgumentParser(description="hithink-finance 本地库 → 量化平台数据")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看本地库状态与覆盖范围")
    exp = sub.add_parser("export", help="导出股票池日线到平台契约目录（个股本地库/ETF远端，自动分流）")
    exp.add_argument("--symbols", nargs="*", default=[], help="6 位代码，如 600519 510300")
    exp.add_argument("--file", default=None, help="股票池文本文件（每行一个代码）")
    exp.add_argument("--start", default="2018-01-01")
    exp.add_argument("--end", default=None)
    exp.add_argument("--adjust", default="qfq", choices=list(_VIEW))
    exp.add_argument("--out", default="data/cn_a/daily")
    exp.add_argument("--no-div-adjust", action="store_true", help="不对 ETF 做现金分红复权")
    exp.add_argument("--outlier", default="keep", choices=["keep", "drop", "interpolate"], help="ETF 疑似错价处理")
    exp.add_argument("--stocks-only", action="store_true", help="不拉 ETF，个股走本地库")
    args = parser.parse_args()

    if args.cmd == "status":
        print("库:", db_status())
        print("覆盖:", coverage())
        print("提示: 更新数据用 `hithink-finance data sync`；本桥接层只读。")
        return

    symbols = list(args.symbols or [])
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            symbols += [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not symbols:
        parser.error("提供 --symbols 或 --file")
    if args.stocks_only:
        written = export_daily(symbols, start=args.start, end=args.end, adjust=args.adjust, out_dir=args.out)
    else:
        written = export_any(symbols, start=args.start, end=args.end, adjust=args.adjust, out_dir=args.out,
                             adjust_dividends=not args.no_div_adjust, outlier_mode=args.outlier)
    print(f"导出 {len(written)} 个标的 → {args.out}")
    total = sum(len(pd.read_parquet(p)) for p in written.values())
    print(f"合计 {total} 行（复权口径: {args.adjust}）")


if __name__ == "__main__":
    main()
