"""Fetch concept index (885xxx.TI) catalog + daily history for the iteration window.

镜像 fetch_industries.py：概念目录 + 概念指数日线（2025-11-03→2026-09-03，
与涨停池 warm-up 窗口一致，pct_5d 自 2025-11 中旬起可用，覆盖 SEL_START）。
产物 raw/concept/catalog_concept.json + hist_885xxx_TI.json。
断点续抓：已存在的文件跳过，可重复运行补缺。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json, ms_at

BASE = Path(__file__).resolve().parent.parent
CON_DIR = BASE / "raw" / "concept"
START, END = "2025-11-03", "2026-09-03"


def main():
    CON_DIR.mkdir(parents=True, exist_ok=True)
    cat_path = CON_DIR / "catalog_concept.json"
    if not cat_path.exists():
        env = run_cli(["index", "catalog", "--tag", "cn_concept"])
        data = env.get("data", {})
        items = data.get("item") if isinstance(data, dict) else data
        save_json(cat_path, items)
    codes = [(i["thscode"], i["name"]) for i in json.loads(cat_path.read_text(encoding="utf-8"))]
    print(f"concepts: {len(codes)}", flush=True)

    ms1, ms2 = ms_at(START), ms_at(END, 23, 59)
    fail = []
    for i, (code, name) in enumerate(codes):
        p = CON_DIR / f"hist_{code.replace('.', '_')}.json"
        if p.exists():
            continue
        try:
            env = run_cli(["index", "history", "--thscode", code,
                           "--start-ms", str(ms1), "--end-ms", str(ms2)])
            save_json(p, env.get("data"))
        except Exception as e:
            fail.append(code)
            print(f"hist FAIL {code} {name}: {e}", flush=True)
        if i % 40 == 0:
            print(f"hist {i}/{len(codes)} fail={len(fail)}", flush=True)
    print(f"DONE fail={len(fail)}/{len(codes)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
