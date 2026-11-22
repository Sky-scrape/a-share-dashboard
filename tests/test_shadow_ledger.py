# -*- coding: utf-8 -*-
"""影子账本脚手架单测：只读内存中的 track，只写 tmp_path（不碰 data/recap）。

覆盖：日期取验证日键（不是 row["date"]）、lu/c7/0<=open<=2 过滤、首建 --since、
幂等、手工列保留、不回插、表头校验、--dry-run 不写盘。
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "strategy-iter" / "scripts" / "shadow_0_2_ledger.py"

_spec = importlib.util.spec_from_file_location("shadow_0_2_ledger", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _pick(code, open_, group="lu", rules="c7", name=None, exec_net=None):
    p = {"code": code, "name": name or code, "group": group,
         "rules": rules, "open": open_}
    if exec_net is not None:
        p["exec_t2"] = {"net": exec_net}
    return p


def _track():
    return {"validations": {
        # key 是验证日 T（实际买入日）；row["date"] 是池日 T-1。
        "20261120": {"date": "20261119", "rules": "c7", "picks": [
            _pick("000001", 1.94, exec_net=-1.234),
            _pick("000002", 2.01),              # 超窗（>2）不收
            _pick("000003", 1.0, group="nlu"),  # 非 lu 不收
            _pick("000004", None),              # open 缺失不收
            _pick("000005", 1.5, rules="c8"),   # 非 c7 不收
        ]},
        "20261121": {"date": "20261120", "rules": "c7", "picks": [
            _pick("000006", 0.0),               # 0.0 是合法窗口边界
        ]},
    }}


def _write_track(tmp_path, track, name="pool_track.json"):
    p = tmp_path / name
    p.write_text(json.dumps(track, ensure_ascii=False), encoding="utf-8")
    return p


def test_filter_and_date_is_key():
    rows = mod.candidates(_track())
    assert len(rows) == 2
    assert sorted(r["date"] for r in rows) == ["20261120", "20261121"]
    # date 必须是 validation 的键；20261119 只是池日
    assert all(r["date"] != "20261119" for r in rows)
    by_code = {r["code"]: r for r in rows}
    assert set(by_code) == {"000001", "000006"}
    assert by_code["000001"]["open"] == "1.94"
    assert by_code["000001"]["note"].startswith("pool=20261119;")
    assert "ref_exec_t2=-1.234" in by_code["000001"]["note"]
    assert by_code["000006"]["open"] == "0.00"
    assert by_code["000006"]["note"] == "pool=20261120;ref_exec_t2=NA"
    assert by_code["000001"]["group"] == "lu"
    assert by_code["000001"]["in_window"] == "true"
    assert by_code["000001"]["fill"] == "unknown"


def test_rules_case_fallback_and_open_type():
    # pick 无 rules 时落回 validation 的 rules；大小写不敏感；bool/字符串不算数值；
    # 2.0 是闭区间上沿，必须收。
    track = {"validations": {
        "20261124": {"date": "20261123", "rules": "C7", "picks": [
            {"code": "000010", "group": "lu", "open": 1.0},
            {"code": "000011", "group": "lu", "open": True},
            {"code": "000012", "group": "lu", "open": "-1"},
            {"code": "000013", "group": "lu", "open": -1.0},
            {"code": "000014", "group": "lu", "open": 2.0},
        ]},
    }}
    rows = mod.candidates(track)
    assert [r["code"] for r in rows] == ["000010", "000014"]
    assert rows[1]["open"] == "2.00"


def test_duplicate_date_code_later_wins():
    track = {"validations": {
        "20261120": {"date": "20261119", "rules": "c7", "picks": [
            _pick("000001", 1.0, name="旧"),
            _pick("000001", 1.5, name="新"),
        ]},
    }}
    rows = mod.candidates(track)
    assert len(rows) == 1
    assert rows[0]["name"] == "新"
    assert rows[0]["open"] == "1.50"


def test_first_run_requires_since(tmp_path):
    ledger = tmp_path / "ledger.csv"
    assert mod.main(["--ledger", str(ledger)]) == 2
    assert not ledger.exists()

    # 只有表头（尚未写入首行）同样算未建账：没有 floor，必须重新给 --since
    ledger.write_text(
        "date,code,name,group,open,in_window,fill,fill_price,size,fee,exit,net,note\n",
        encoding="utf-8-sig")
    assert mod.main(["--ledger", str(ledger)]) == 2


def test_first_run_with_since(tmp_path):
    ledger = tmp_path / "ledger.csv"
    track = _write_track(tmp_path, _track())
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120"]) == 0
    rows = mod.load_ledger(ledger)
    assert len(rows) == 2
    assert [r["date"] for r in rows] == ["20261120", "20261121"]
    assert [r["fill"] for r in rows] == ["unknown", "unknown"]
    assert [r["net"] for r in rows] == ["", ""]
    assert [r["open"] for r in rows] == ["1.94", "0.00"]

    # ISO 写法等价
    ledger_iso = tmp_path / "ledger_iso.csv"
    assert mod.main(["--track", str(track), "--ledger", str(ledger_iso),
                     "--since", "2026-11-20"]) == 0
    assert mod.load_ledger(ledger_iso) == rows


def test_idempotent(tmp_path):
    ledger = tmp_path / "ledger.csv"
    track = _write_track(tmp_path, _track())
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120"]) == 0
    before = mod.load_ledger(ledger)
    assert len(before) == 2

    assert mod.main(["--track", str(track), "--ledger", str(ledger)]) == 0
    after = mod.load_ledger(ledger)
    assert len(after) == 2
    _rows, added = mod.merge(before, mod.candidates(_track()))
    assert added == 0


def test_preserve_manual(tmp_path):
    ledger = tmp_path / "ledger.csv"
    track = _write_track(tmp_path, _track())
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120"]) == 0

    rows = mod.load_ledger(ledger)
    row = next(r for r in rows if r["code"] == "000001")
    row.update({"fill": "yes", "fill_price": "12.34", "size": "1000",
                "fee": "1.85", "exit": "12.90", "net": "2.10",
                "note": "manual"})
    mod.write_ledger(ledger, rows)
    manual = dict(row)

    track2 = _track()
    track2["validations"]["20261123"] = {
        "date": "20261120", "rules": "c7", "picks": [_pick("000007", 1.2)]}
    assert mod.main(["--track", str(_write_track(tmp_path, track2)),
                     "--ledger", str(ledger)]) == 0

    after = mod.load_ledger(ledger)
    assert len(after) == 3
    kept = next(r for r in after if r["code"] == "000001")
    assert kept == manual
    assert "000007" in {r["code"] for r in after}


def test_no_back_insert(tmp_path):
    ledger = tmp_path / "ledger.csv"
    track = _write_track(tmp_path, _track())
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120"]) == 0
    before = len(mod.load_ledger(ledger))

    old = copy.deepcopy(_track())
    old["validations"]["20261101"] = {
        "date": "20261031", "rules": "c7", "picks": [_pick("000009", 1.0)]}
    assert mod.main(["--track", str(_write_track(tmp_path, old, "pool_old.json")),
                     "--ledger", str(ledger)]) == 0

    rows = mod.load_ledger(ledger)
    assert len(rows) == before
    assert all(r["date"] != "20261101" for r in rows)
    assert all(r["code"] != "000009" for r in rows)


def test_header_mismatch_raises(tmp_path):
    ledger = tmp_path / "ledger.csv"
    ledger.write_text("date,code\n20261120,000001\n", encoding="utf-8-sig")
    with pytest.raises(ValueError):
        mod.load_ledger(ledger)

    before = ledger.read_bytes()
    track = _write_track(tmp_path, _track())
    rc = mod.main(["--track", str(track), "--ledger", str(ledger)])
    assert rc != 0
    assert ledger.read_bytes() == before


def test_dry_run_writes_nothing(tmp_path):
    ledger = tmp_path / "sub" / "ledger.csv"
    track = _write_track(tmp_path, _track())
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120", "--dry-run"]) == 0
    assert not ledger.exists()
    assert not ledger.parent.exists()

    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--since", "20261120"]) == 0
    before = ledger.read_bytes()
    assert mod.main(["--track", str(track), "--ledger", str(ledger),
                     "--dry-run"]) == 0
    assert ledger.read_bytes() == before
