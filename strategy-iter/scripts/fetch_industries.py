"""Fetch industry index constituents + daily history, plus major index history.

窗口日期唯一来源（2026-09-12 修正，镜像 fetch_concepts）：START=WIN_START、
END=SEL_END 的次一交易日（T+1 上界，本地库现取）——此前 END 硬编码
"2026-09-03"，SEL_END 外推时本脚本没跟上（缺口见 fetch_concepts docstring）。

--update 增量补尾：已存在的 hist/idx 文件只抓尾部并按 date_ms 去重合并；
成分股 cons 文件不随窗口变化，维持 skip-if-exists。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json, load_json, ms_at, next_trade_date

BASE = Path(__file__).resolve().parent.parent
IND_DIR = BASE / "raw" / "ind"
IDX_DIR = BASE / "raw" / "idx"
sys.path.insert(0, str(BASE))
from engine.data import WIN_START, SEL_END  # noqa: E402  窗口日期唯一来源

START, END = WIN_START, next_trade_date(SEL_END)
IDX_START = "2025-06-02"

MAJOR = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
         "000905.SH", "000852.SH", "000688.SH"]


def _update_hist(p: Path, code, ms1, ms2):
    """已有文件只补尾部（date_ms 去重合并排序）；空文件整段重抓。"""
    d = load_json(p)
    items = d.get("item") or []
    if not items:
        env = run_cli(["index", "history", "--thscode", code,
                       "--start-ms", str(ms1), "--end-ms", str(ms2)])
        save_json(p, env.get("data"))
        return "refilled"
    last_ms = max(i["date_ms"] for i in items)
    if last_ms >= ms_at(END, 23, 59):
        return "fresh"
    env = run_cli(["index", "history", "--thscode", code,
                   "--start-ms", str(last_ms + 1), "--end-ms", str(ms2)])
    new = (env.get("data") or {}).get("item") or []
    have = {i["date_ms"] for i in items}
    add = [i for i in new if i["date_ms"] not in have]
    if add:
        d["item"] = sorted(items + add, key=lambda x: x["date_ms"])
        save_json(p, d)
    return f"+{len(add)}"


def main(update=False):
    # 1) industry catalog
    cat_path = IND_DIR / "catalog_industry.json"
    if not cat_path.exists():
        env = run_cli(["index", "catalog", "--tag", "industry"])
        data = env.get("data", {})
        items = data.get("item") if isinstance(data, dict) else data
        save_json(cat_path, items)
    codes = [(i["thscode"], i["name"]) for i in (load(cat_path))]
    print(f"industries: {len(codes)} window {START}..{END}", flush=True)

    # 2) constituents per industry（与窗口无关，缺才抓）
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
    added = 0
    for i, (code, name) in enumerate(codes):
        p = IND_DIR / f"hist_{code.replace('.', '_')}.json"
        try:
            if p.exists():
                if not update:
                    continue
                tag = _update_hist(p, code, ms1, ms2)
                if tag.startswith("+") or tag == "refilled":
                    added += 1
                    print(f"hist {code} {name}: {tag}", flush=True)
                continue
            env = run_cli(["index", "history", "--thscode", code,
                           "--start-ms", str(ms1), "--end-ms", str(ms2)])
            save_json(p, env.get("data"))
        except Exception as e:
            print(f"hist FAIL {code} {name}: {e}", flush=True)
        if i % 40 == 0:
            print(f"hist {i}/{len(codes)}", flush=True)

    # 4) major indices (longer window)
    ms1i = ms_at(IDX_START)
    idx_added = 0
    for code in MAJOR:
        p = IDX_DIR / f"hist_{code.replace('.', '_')}.json"
        try:
            if p.exists():
                if not update:
                    continue
                tag = _update_hist(p, code, ms1i, ms2)
                if tag.startswith("+") or tag == "refilled":
                    idx_added += 1
                    print(f"idx {code}: {tag}", flush=True)
                continue
            env = run_cli(["index", "history", "--thscode", code,
                           "--start-ms", str(ms1i), "--end-ms", str(ms2)])
            save_json(p, env.get("data"))
            n = len((env.get("data") or {}).get("item") or [])
            print(f"idx {code} n={n}", flush=True)
        except Exception as e:
            print(f"idx FAIL {code}: {e}", flush=True)
    print(f"DONE updated={added if update else 'n/a'} idx_updated={idx_added if update else 'n/a'}")


def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(update="--update" in sys.argv)
