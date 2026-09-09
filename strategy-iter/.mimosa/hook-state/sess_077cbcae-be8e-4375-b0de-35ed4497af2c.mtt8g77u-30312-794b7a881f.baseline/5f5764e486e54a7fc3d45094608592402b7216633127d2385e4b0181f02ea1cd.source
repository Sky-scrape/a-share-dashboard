"""Export local DuckDB daily kline window to parquet (raw + qfq)."""
import os
import sys
from pathlib import Path
import duckdb

BASE = Path(__file__).resolve().parent.parent
# hithink-finance CLI 的标准库位置按机器而异：默认从 LOCALAPPDATA 解析，HITHINK_DB 可覆盖
DB = os.environ.get("HITHINK_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "hithink-finance", "data", "market.duckdb")
START = "2024-09-01"
END = "2026-09-03"


def main():
    con = duckdb.connect(DB, read_only=True)
    out1 = BASE / "data" / "daily_raw.parquet"
    con.execute(f"""
        COPY (
          SELECT thscode, date, open, high, low, close, prev_close, volume, amount
          FROM raw_kline_daily
          WHERE date >= DATE '{START}' AND date <= DATE '{END}'
        ) TO '{out1}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n1 = con.execute(f"SELECT COUNT(*) FROM '{out1}'").fetchone()[0]
    print("daily_raw rows:", n1, flush=True)

    out2 = BASE / "data" / "daily_qfq.parquet"
    con.execute(f"""
        COPY (
          SELECT thscode, date, open, high, low, close, volume, amount
          FROM v_daily_qfq
          WHERE date >= DATE '{START}' AND date <= DATE '{END}'
        ) TO '{out2}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n2 = con.execute(f"SELECT COUNT(*) FROM '{out2}'").fetchone()[0]
    print("daily_qfq rows:", n2, flush=True)

    # trade dates list
    dates = [r[0] for r in con.execute(
        f"SELECT DISTINCT strftime(date,'%Y-%m-%d') FROM raw_kline_daily "
        f"WHERE date >= DATE '{START}' AND date <= DATE '{END}' ORDER BY 1").fetchall()]
    with open(BASE / "data" / "trade_dates.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(dates))
    print("trade dates:", len(dates), dates[0], "..", dates[-1], flush=True)
    con.close()


if __name__ == "__main__":
    main()
