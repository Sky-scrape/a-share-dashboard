# -*- coding: utf-8 -*-
"""影子账本脚手架：lu 0-2% 反事实（复盘 §9 N2）。

只读 ``data/recap/pool_track.json``，按验证日补齐
``strategy-iter/runs/shadow_0_2/ledger.csv``。预注册口径（本脚本不做任何统计判决）：

- 只收 lu 组、pick 或 validation 行的 rules（不区分大小写）== ``c7``、
  open 为数值（非 bool）且 ``0 <= open <= 2``；
- ``date`` 取 validations 字典的键（验证日 T = 实际买入日）。``row["date"]`` 是
  池日 T-1（``record_validation`` 按 date8 键覆盖写，``validate_prev_pool(date8)``
  取 T 日 OHLC），只写进 note 的 ``pool=`` 前缀，绝不拿它当主键；
- 新行 ``fill=unknown``，fill_price/size/fee/exit/net 留空，由人工按真实成交回填；
- 首建必须显式 ``--since``（预注册起点）；已有账本永不回插早于最早日期的候选；
- 重跑按 ``(date, code)`` 去重，已有行逐字段原样保留；写盘同目录 tmp + ``os.replace``。

用法::

    python strategy-iter/scripts/shadow_0_2_ledger.py --since 20261122
    python strategy-iter/scripts/shadow_0_2_ledger.py            # 重跑：只补新行
    python strategy-iter/scripts/shadow_0_2_ledger.py --dry-run  # 只预览，不写盘
"""
import argparse
import csv
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACK = ROOT / "data" / "recap" / "pool_track.json"
DEFAULT_LEDGER = ROOT / "strategy-iter" / "runs" / "shadow_0_2" / "ledger.csv"

FIELDS = [
    "date", "code", "name", "group", "open", "in_window", "fill",
    "fill_price", "size", "fee", "exit", "net", "note",
]


def candidates(track):
    """从 pool_track 抽出预注册候选（lu / c7 / 0 <= open <= 2），按 (date, code) 排序。

    ``date`` 用 validations 的键（验证日 T，实际买入日）；``row["date"]`` 是池日
    T-1，只写进 note。同一 ``(date, code)`` 后者覆盖前者。
    """
    out = {}
    for key, val in (track.get("validations") or {}).items():
        if not isinstance(val, dict):
            continue
        date = str(key)
        pool_date = str(val.get("date") or "")
        val_rules = val.get("rules")
        for p in val.get("picks") or []:
            if not isinstance(p, dict):
                continue
            rules = p.get("rules") or val_rules or ""
            if str(rules).strip().lower() != "c7":
                continue
            if p.get("group") != "lu":
                continue
            o = p.get("open")
            if isinstance(o, bool) or not isinstance(o, (int, float)):
                continue
            if not (0 <= o <= 2):
                continue
            code = str(p.get("code") or "")
            et = p.get("exec_t2")
            net = (et or {}).get("net") if isinstance(et, dict) else None
            note = f"pool={pool_date};" + (
                f"ref_exec_t2={net}" if net is not None else "ref_exec_t2=NA")
            out[(date, code)] = {
                "date": date,
                "code": code,
                "name": p.get("name") or "",
                "group": "lu",
                "open": f"{float(o):.2f}",
                "in_window": "true",
                "fill": "unknown",
                "fill_price": "",
                "size": "",
                "fee": "",
                "exit": "",
                "net": "",
                "note": note,
            }
    return sorted(out.values(), key=lambda r: (r["date"], r["code"]))


def load_ledger(path):
    """读账本；文件不存在返回 []，表头不符 raise ValueError（拒绝静默重写）。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        if header != FIELDS:
            got = ",".join(header) if header else "(空/无表头)"
            raise ValueError(
                f"账本表头不符：期望 {','.join(FIELDS)}，实际 {got}")
        return [dict(r) for r in reader]


def merge(existing, cands):
    """合并候选，返回 ``(rows, added_count)``。

    floor = 已有账本最早 date：早于它的候选一律丢弃（防 --since 建账后历史样本
    回插）。已有行逐字段原样保留；新行按 ``(date, code)`` 补缺；最后排序。
    """
    dates = [str(r.get("date") or "") for r in existing if r.get("date")]
    floor = min(dates) if dates else None
    rows = [dict(r) for r in existing]
    seen = {(str(r.get("date") or ""), str(r.get("code") or "")) for r in existing}
    added = 0
    for c in cands:
        date = str(c.get("date") or "")
        code = str(c.get("code") or "")
        if floor is not None and date < floor:
            continue
        if (date, code) in seen:
            continue
        rows.append(dict(c))
        seen.add((date, code))
        added += 1
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("code") or "")))
    return rows, added


def write_ledger(path, rows):
    """原子写账本：同目录临时文件 + os.replace，utf-8-sig（Excel 直接打开不乱码）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for r in rows:
                writer.writerow(
                    {k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="影子账本 lu 0-2%（复盘 §9 N2）：读 pool_track，按验证日补账")
    ap.add_argument("--track", default=str(DEFAULT_TRACK),
                    help="pool_track.json 路径（只读）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help="ledger.csv 路径（只写此文件）")
    ap.add_argument("--since",
                    help="预注册起点 YYYYMMDD 或 YYYY-MM-DD；首建必填")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args(argv)

    ledger = Path(args.ledger)
    try:
        existing = load_ledger(ledger)
    except ValueError as e:
        print(f"[error] {e}")
        return 2

    since = (args.since or "").strip().replace("-", "")
    # 不存在或只有表头（尚未写入首行）都算首建：必须显式起点，否则无法挡住
    # 历史样本回插，也会让下一次运行失去 floor。
    if (not ledger.exists() or not existing) and not since:
        print("首次建账必须显式指定预注册起点：--since YYYYMMDD（或 YYYY-MM-DD）")
        return 2

    try:
        track = json.loads(Path(args.track).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[error] 读取 pool_track 失败：{e}")
        return 1

    cands = candidates(track)
    if since:
        cands = [c for c in cands if c["date"] >= since]
    rows, added = merge(existing, cands)

    old_keys = {(str(r.get("date") or ""), str(r.get("code") or ""))
                for r in existing}
    new_rows = [r for r in rows
                if (r["date"], r["code"]) not in old_keys]

    if args.dry_run:
        print(f"[dry-run] 候选 {len(cands)} 行；将新增 {added} 行；"
              f"现有 {len(existing)} 行；不写盘")
        for r in new_rows:
            print(f"  + {r['date']} {r['code']} {r['name']} "
                  f"open={r['open']} {r['note']}")
        return 0

    write_ledger(ledger, rows)
    print(f"[ok] 新增 {added} 行；账本共 {len(rows)} 行 -> {ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
