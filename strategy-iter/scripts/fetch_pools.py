"""Fetch per-day limit-up pool (all pages) and dragon-tiger list for the window."""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json, load_json, ms_at

BASE = Path(__file__).resolve().parent.parent
# hithink-finance CLI 的标准库位置按机器而异：默认从 LOCALAPPDATA 解析，HITHINK_DB 可覆盖
DB = os.environ.get("HITHINK_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "hithink-finance", "data", "market.duckdb")
POOL_DIR = BASE / "raw" / "pool"
DT_DIR = BASE / "raw" / "dt"
START, END = "2025-11-03", "2026-09-03"
for _v, _k in ((START, "起始日期"), (END, "结束日期")):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", _v):
        raise ValueError(f"非法{_k}: {_v!r}")  # SQL 绑定值口径校验（防跨文件污点）


def fetch_limit_up(date):
    out = POOL_DIR / f"lu_{date.replace('-', '')}.json"
    if out.exists():
        return "skip"
    ms = ms_at(date)
    items, page, total = [], 1, None
    while True:
        env = run_cli(["special", "limit-up-pool", "--date-ms", str(ms),
                       "--page", str(page), "--size", "200"])
        data = env.get("data", {})
        got = data.get("item") or []
        items.extend(got)
        pag = data.get("pagination") or {}
        total = pag.get("total", len(items))
        pages = pag.get("pages", 1)
        if page >= pages or not got:
            break
        page += 1
    save_json(out, {"trade_date": date, "total": total, "items": items})
    return f"ok n={len(items)}"


def fetch_dt(date):
    out = DT_DIR / f"dt_{date.replace('-', '')}.json"
    if out.exists():
        return "skip"
    env = run_cli(["special", "dragon-tiger", "--date", date,
                   "--board-type", "all"])
    save_json(out, env.get("data"))
    data = env.get("data") or {}
    return f"ok n={data.get('stock_count')}"


def main():
    # 交易日查询内联（参数绑定，无跨文件 SQL 入口；口径与 common.trading_days 一致）
    import duckdb
    _con = duckdb.connect(DB, read_only=True)
    days = [r[0] for r in _con.execute(
        "SELECT DISTINCT strftime(date,'%Y-%m-%d') d FROM raw_kline_daily "
        "WHERE date >= ? AND date <= ? ORDER BY 1", [START, END]).fetchall()]
    _con.close()
    print(f"trading days: {len(days)} ({days[0]} .. {days[-1]})", flush=True)
    for i, d in enumerate(days):
        try:
            r1 = fetch_limit_up(d)
        except Exception as e:
            r1 = f"FAIL {e}"
        try:
            r2 = fetch_dt(d)
        except Exception as e:
            r2 = f"FAIL {e}"
        if i % 5 == 0 or "FAIL" in (r1 + r2):
            print(f"[{i+1}/{len(days)}] {d} pool={r1} dt={r2}", flush=True)
    print("DONE")


if __name__ == "__main__":
    main()
