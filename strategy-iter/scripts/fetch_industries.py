"""Fetch industry index constituents + daily history, plus major index history."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json, ms_at

BASE = Path(__file__).resolve().parent.parent
IND_DIR = BASE / "raw" / "ind"
IDX_DIR = BASE / "raw" / "idx"
START, END = "2025-11-03", "2026-09-03"
IDX_START = "2025-06-02"

MAJOR = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
         "000905.SH", "000852.SH", "000688.SH"]


def main():
    # 1) industry catalog
    cat_path = IND_DIR / "catalog_industry.json"
    if not cat_path.exists():
        env = run_cli(["index", "catalog", "--tag", "industry"])
        data = env.get("data", {})
        items = data.get("item") if isinstance(data, dict) else data
        save_json(cat_path, items)
    codes = [(i["thscode"], i["name"]) for i in (load(cat_path))]
    print(f"industries: {len(codes)}", flush=True)

    # 2) constituents per industry
    for i, (code, name) in enumerate(codes):
        p = IND_DIR / f"cons_{code.replace('.', '_')}.json"
        if p.exists():
            continue
        try:
            env = run_cli(["index", "constituents", "--thscode", code])
            save_json(p, env.get("data"))
        except Exception as e:
            print(f"cons FAIL {code}: {e}", flush=True)
        if i % 40 == 0:
            print(f"cons {i}/{len(codes)}", flush=True)

    # 3) industry history
    ms1, ms2 = ms_at(START), ms_at(END, 23, 59)
    for i, (code, name) in enumerate(codes):
        p = IND_DIR / f"hist_{code.replace('.', '_')}.json"
        if p.exists():
            continue
        try:
            env = run_cli(["index", "history", "--thscode", code,
                           "--start-ms", str(ms1), "--end-ms", str(ms2)])
            save_json(p, env.get("data"))
        except Exception as e:
            print(f"hist FAIL {code}: {e}", flush=True)
        if i % 40 == 0:
            print(f"hist {i}/{len(codes)}", flush=True)

    # 4) major indices (longer window)
    ms1i = ms_at(IDX_START)
    for code in MAJOR:
        p = IDX_DIR / f"hist_{code.replace('.', '_')}.json"
        if p.exists():
            continue
        try:
            env = run_cli(["index", "history", "--thscode", code,
                           "--start-ms", str(ms1i), "--end-ms", str(ms2)])
            save_json(p, env.get("data"))
            n = len((env.get("data") or {}).get("item") or [])
            print(f"idx {code} n={n}", flush=True)
        except Exception as e:
            print(f"idx FAIL {code}: {e}", flush=True)
    print("DONE")


def load(p):
    import json
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
