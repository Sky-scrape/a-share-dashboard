"""Fetch full A-share symbol table (with names) for SH/SZ/BJ."""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json

OUT = Path(__file__).resolve().parent.parent / "data" / "symbols.csv"


def main():
    rows = []
    offset = 0
    limit = 10000
    while True:
        env = run_cli(["symbol", "list", "--exchange", "SH,SZ,BJ",
                       "--asset-type", "a-share",
                       "--limit", str(limit), "--offset", str(offset)])
        data = env.get("data", {})
        items = data.get("item") if isinstance(data, dict) else data
        if not items:
            break
        for it in items:
            rows.append({
                "thscode": it.get("thscode"),
                "ticker": it.get("ticker"),
                "name": it.get("name"),
                "exchange": it.get("exchange"),
            })
        print(f"offset={offset} got={len(items)} total={len(rows)}", flush=True)
        if len(items) < limit:
            break
        offset += limit
        if offset > 30000:
            break
    OUT.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["thscode", "ticker", "name", "exchange"])
    w.writeheader()
    w.writerows(rows)
    OUT.write_text(buf.getvalue(), encoding="utf-8")
    print(f"SAVED {OUT} rows={len(rows)}")


if __name__ == "__main__":
    main()
