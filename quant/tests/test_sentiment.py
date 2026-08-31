"""情绪面板：池解析/缓存/趋势表（全 monkeypatch，不打真 CLI）。"""
import json

import pandas as pd
import pytest

from quant_sim.research import sentiment as senti


def _fake_cli_factory(counters):
    def fake_cli(args, timeout=300):
        cmd = args[1]
        if cmd == "limit-up-pool":
            items, total = counters["up"][int(args[3])]
            return {"item": items, "pagination": {"total": total, "pages": 1, "size": 200, "page": 1}}
        if cmd == "limit-break-pool":
            return {"item": [], "pagination": {"total": counters["break"][int(args[3])], "pages": 0}}
        if cmd == "limit-down-pool":
            return {"item": [], "pagination": {"total": counters["down"][int(args[3])], "pages": 0}}
        raise AssertionError(cmd)

    return fake_cli


D1 = pd.Timestamp("2026-08-27")
D2 = pd.Timestamp("2026-08-28")
MS1 = int(D1.tz_localize("Asia/Shanghai").timestamp() * 1000)
MS2 = int(D2.tz_localize("Asia/Shanghai").timestamp() * 1000)

UP1 = [  # 8/27 涨停池 3 家（total=3）
    {"name": "甲", "continue_day_cnt": 1, "seal_money": 2e8, "is_st": False, "limit_up_reason": "重组", "limit_up_time": "09:30"},
    {"name": "乙", "continue_day_cnt": 2, "seal_money": 9e8, "is_st": False, "limit_up_reason": "AI", "limit_up_time": "10:00"},
    {"name": "丙", "continue_day_cnt": 1, "seal_money": 1e8, "is_st": True, "limit_up_reason": "", "limit_up_time": "14:00"},
]
UP2 = [  # 8/28 涨停池 2 家：一家晋级（cnt=2）一家首板
    {"name": "乙", "continue_day_cnt": 3, "seal_money": 12e8, "is_st": False, "limit_up_reason": "AI", "limit_up_time": "09:35"},
    {"name": "丁", "continue_day_cnt": 1, "seal_money": 3e8, "is_st": False, "limit_up_reason": "核电", "limit_up_time": "11:00"},
]
COUNTERS = {
    "up": {MS1: (UP1, 3), MS2: (UP2, 2)},
    "break": {MS1: 2, MS2: 1},
    "down": {MS1: 3, MS2: 0},
}


@pytest.fixture()
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(senti, "_cli", _fake_cli_factory(COUNTERS))
    monkeypatch.setattr(senti, "CACHE_DIR", tmp_path)
    return tmp_path


def test_fetch_day_metrics(patched):
    raw = senti.fetch_day(D2)
    assert raw["n_up"] == 2 and raw["n_break"] == 1 and raw["n_down"] == 0
    assert raw["max_lianban"] == 3
    assert raw["ladder"] == {"3": 1, "1": 1}
    assert raw["n_lianban_2plus"] == 1 and raw["st_up"] == 0
    assert raw["top"][0]["name"] == "乙" and raw["top"][0]["seal_yi"] == 12.0


def test_frame_and_promo(patched):
    senti.update_cache([D1, D2])
    assert senti.update_cache([D1, D2])["cached"] == 2  # 二次全命中
    fr = senti.sentiment_frame([D1, D2])
    assert list(fr.index.date) == [D1.date(), D2.date()]
    row1, row2 = fr.iloc[0], fr.iloc[1]
    assert row1["炸板率"] == pytest.approx(2 / 5)
    assert pd.isna(row1["晋级率"]) or row1["晋级率"] is None  # 首日无昨日基准
    assert row2["晋级率"] == pytest.approx(1 / 3, abs=1e-3)  # 今日≥2 的 1 家 / 昨日涨停 3 家
    assert senti.latest_raw([D1, D2])["date"] == "2026-08-28"


def test_cli_failure_not_cached(patched, monkeypatch):
    def boom(args, timeout=300):
        raise RuntimeError("network down")

    monkeypatch.setattr(senti, "_cli", boom)
    stat = senti.update_cache([D2])
    assert stat["fetched"] == 0 and len(stat["failures"]) == 1
    assert senti.load_cached_day(D2) is None
