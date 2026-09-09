"""Export local DuckDB daily kline window to parquet (raw + qfq)."""
import datetime
import os
import re
from pathlib import Path
import duckdb

BASE = Path(__file__).resolve().parent.parent
# hithink-finance CLI 的标准库位置按机器而异：默认从 LOCALAPPDATA 解析，HITHINK_DB 可覆盖
DB = os.environ.get("HITHINK_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "hithink-finance", "data", "market.duckdb")
START = "2024-09-01"
END = "2026-09-03"


def _validated(v: str, kind: str) -> str:
    """日期绑定值严格校验（ISO 日历日），非法即拒绝。"""
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"非法{kind}: {v!r}")
    datetime.date.fromisoformat(v)
    return v


def main():
    start, end = _validated(START, "起始日期"), _validated(END, "结束日期")
    # SQL 全部为固定字面量，日期经参数绑定传入（无任何拼接）；导出走
    # DataFrame.to_parquet，路径由 BASE + 字面量文件名构造，不可偏航。
    con = duckdb.connect(DB, read_only=True)

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
