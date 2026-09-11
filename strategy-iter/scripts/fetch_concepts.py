"""Fetch concept index (885xxx.TI) catalog + daily history for the iteration window.

镜像 fetch_industries.py：概念目录 + 概念指数日线。产物
raw/concept/catalog_concept.json + hist_885xxx_TI.json。
断点续抓：已存在的文件跳过，可重复运行补缺。

窗口日期唯一来源（2026-09-12 修正）：START=WIN_START、END=SEL_END 的次一交易日
（T+1 上界）——此前 END 硬编码 "2026-09-03"，SEL_END 外推到 09-08 时本脚本没跟上，
09-04..09-08 的选股日在概念因子上降级运行（C8/C9 窗口的隐性数据缺口，已补）。

--update 增量补尾：对已存在的 hist 文件只抓「本地最后日期次日 → END」的尾部并
按 date_ms 去重合并（不整段重抓）；文件缺尾部是窗口外推后的常态，更新入口。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import run_cli, save_json, load_json, ms_at, next_trade_date

BASE = Path(__file__).resolve().parent.parent
CON_DIR = BASE / "raw" / "concept"
sys.path.insert(0, str(BASE))
from engine.data import WIN_START, SEL_END  # noqa: E402  窗口日期唯一来源

START, END = WIN_START, next_trade_date(SEL_END)


def fetch_full(code, ms1, ms2):
    env = run_cli(["index", "history", "--thscode", code,
                   "--start-ms", str(ms1), "--end-ms", str(ms2)])
    save_json(CON_DIR / f"hist_{code.replace('.', '_')}.json", env.get("data"))


def update_tail(p: Path, code, ms2):
    """已有文件只补尾部：last(date_ms)+1 → ms2，去重合并按日期排序。"""
    d = load_json(p)
    items = d.get("item") or []
    if not items:
        fetch_full(code, ms_at(START), ms2)
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
    CON_DIR.mkdir(parents=True, exist_ok=True)
    cat_path = CON_DIR / "catalog_concept.json"
    if not cat_path.exists():
        env = run_cli(["index", "catalog", "--tag", "cn_concept"])
        data = env.get("data", {})
        items = data.get("item") if isinstance(data, dict) else data
        save_json(cat_path, items)
    codes = [(i["thscode"], i["name"]) for i in json.loads(cat_path.read_text(encoding="utf-8"))]
    print(f"concepts: {len(codes)} window {START}..{END}", flush=True)

    ms1, ms2 = ms_at(START), ms_at(END, 23, 59)
    fail, added = [], 0
    for i, (code, name) in enumerate(codes):
        p = CON_DIR / f"hist_{code.replace('.', '_')}.json"
        try:
            if p.exists():
                if not update:
                    continue
                tag = update_tail(p, code, ms2)
                if tag.startswith("+") or tag == "refilled":
                    added += 1
                    print(f"hist {code} {name}: {tag}", flush=True)
                continue
            fetch_full(code, ms1, ms2)
        except Exception as e:
            fail.append(code)
            print(f"hist FAIL {code} {name}: {e}", flush=True)
        if i % 40 == 0:
            print(f"hist {i}/{len(codes)} fail={len(fail)}", flush=True)
    print(f"DONE fail={len(fail)}/{len(codes)} updated={added if update else 'n/a'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(update="--update" in sys.argv)
