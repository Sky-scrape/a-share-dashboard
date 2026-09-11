"""Export local DuckDB daily kline window to parquet (raw + qfq)."""
import datetime
import os
import re
import sys
from pathlib import Path
import duckdb

BASE = Path(__file__).resolve().parent.parent
# hithink-finance CLI 的标准库位置按机器而异：默认从 LOCALAPPDATA 解析，HITHINK_DB 可覆盖
DB = os.environ.get("HITHINK_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "hithink-finance", "data", "market.duckdb")

sys.path.insert(0, str(BASE))
from engine.data import SEL_END  # noqa: E402  窗口日期唯一来源（engine/data.py）

START = "2024-09-01"   # 前复权暖机历史，早于选股窗口，独立口径


def _next_trade_date(con) -> str:
    """导出上界 = SEL_END 的次一交易日（T+1 走势验证需要）。

    SEL_END 见 engine/data.py（唯一日期源）；T+1 不再另存一份硬编码，直接从本地
    库现取 SEL_END 之后首个有日线的交易日（MIN 保证恰好 T+1，库更新再多也不多导）。
    库还没同步到 T+1 时报错——宁可显式失败，不静默截短验证窗口。
    """
    row = con.execute(
        "SELECT MIN(DISTINCT strftime(date,'%Y-%m-%d')) FROM raw_kline_daily "
        "WHERE date > CAST(? AS DATE)", [SEL_END]).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"本地库尚无 {SEL_END} 之后的交易日数据，请先同步行情再导出")
    return row[0]


def _validated(v: str, kind: str) -> str:
    """日期绑定值严格校验（ISO 日历日），非法即拒绝。"""
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"非法{kind}: {v!r}")
    datetime.date.fromisoformat(v)
    return v


def main():
    con = duckdb.connect(DB, read_only=True)
    start = _validated(START, "起始日期")
    end = _validated(_next_trade_date(con), "结束日期")
    # SQL 全部为固定字面量，日期经参数绑定传入（无任何拼接）；导出走
    # DataFrame.to_parquet，路径由 BASE + 字面量文件名构造，不可偏航。

    df_raw = con.execute(
        "SELECT thscode, date, open, high, low, close, prev_close, volume, amount "
        "FROM raw_kline_daily WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
        "ORDER BY thscode, date",
        [start, end]).df()
    out_raw = Path(BASE) / "data" / "daily_raw.parquet"
    out_raw.parent.mkdir(parents=True, exist_ok=True)
    df_raw.to_parquet(out_raw, index=False, compression="zstd")
    print("daily_raw rows:", len(df_raw), flush=True)

    df_qfq = con.execute(
        "SELECT thscode, date, open, high, low, close, volume, amount "
        "FROM v_daily_qfq WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
        "ORDER BY thscode, date",
        [start, end]).df()
    out_qfq = Path(BASE) / "data" / "daily_qfq.parquet"
    df_qfq.to_parquet(out_qfq, index=False, compression="zstd")
    print("daily_qfq rows:", len(df_qfq), flush=True)

    # trade dates list
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT strftime(date,'%Y-%m-%d') FROM raw_kline_daily "
        "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) ORDER BY 1",
        [start, end]).fetchall()]
    (Path(BASE) / "data" / "trade_dates.txt").write_text(
        "\n".join(dates), encoding="utf-8")
    print("trade dates:", len(dates), dates[0], "..", dates[-1], flush=True)
    con.close()


if __name__ == "__main__":
    main()
