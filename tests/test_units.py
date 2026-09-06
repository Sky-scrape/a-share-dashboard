# -*- coding: utf-8 -*-
"""backend 纯函数单元测试（2026-09-04 新增）。

背景：此前全项目唯一测试是 tests/smoke.py 全链路冒烟 + 大量「读源码文本断言」
（能防函数被误删，不验行为）。这里对最值得保护的纯计算逻辑补真正的行为测试：
代码转换、交易日历口径、M_Final 环境分档/分桶、主板范围、备选池打分与买点文案、
共享重试。全部离线、不联网、不写数据目录。
"""
import pytest

if __package__ in (None, ""):
    # 直接 `python tests/test_units.py` 时没有 pytest 的 conftest 引导，
    # 手动导入一次（其副作用就是把项目根与 backend 各目录放进 sys.path），
    # 避免首行扁平 import 就 ModuleNotFoundError；pytest 路径下此导入无副作用。
    import conftest  # noqa: F401


# ---------------- thscodes：6 位代码 → thscode（单一来源） ----------------

from thscodes import to_thscode  # noqa: E402


@pytest.mark.parametrize("code,expect", [
    ("600519", "600519.SH"), ("000001", "000001.SZ"), ("300750", "300750.SZ"),
    ("688981", "688981.SH"), ("002415", "002415.SZ"),
    ("510300", "510300.SH"), ("159915", "159915.SZ"),          # 基金
    ("900901", "900901.SH"), ("200002", "200002.SZ"),          # B 股
    ("830799", "830799.BJ"), ("430047", "430047.BJ"),
    ("920001", "920001.BJ"),                                    # 北交所新段（9 前先判 92）
    ("sh600519", "600519.SH"), ("SZ000001", "000001.SZ"),
    ("600519.SH", "600519.SH"), (" 000001 ", "000001.SZ"),
])
def test_to_thscode_ok(code, expect):
    assert to_thscode(code) == expect


@pytest.mark.parametrize("bad", ["", None, "茅台", "6005", "6005199", "abc123"])
def test_to_thscode_invalid(bad):
    assert to_thscode(bad) is None


# ---------------- trade_cal：交易日历口径（格式不一致事故防回退） ----------------

from trade_cal import is_trade_today  # noqa: E402


def test_trade_cal_today_in():
    assert is_trade_today(today8="20260904",
                          cal=lambda: {"item": [{"date": "20260901"},
                                                {"date": "20260904"}]}) is True


def test_trade_cal_not_in():
    assert is_trade_today(today8="20260904",
                          cal=lambda: {"item": [{"date": "20260102"},
                                                {"date": "20260105"}]}) is False


def test_trade_cal_dashed_dates_still_match():
    # 上游格式带横杠也不能把交易日判成休市（2026-08-31 静默停摆事故）
    assert is_trade_today(today8="20260904",
                          cal=lambda: {"item": [{"date": "2026-09-04"}]}) is True


def test_trade_cal_schema_change_returns_none():
    # 拿不到可用日期 → None（调用方按交易日继续），绝不返回 False 静默跳过
    assert is_trade_today(today8="20260904",
                          cal=lambda: {"item": [{"trade_day": "20260904"}]}) is None


def test_trade_cal_error_returns_none():
    def boom():
        raise RuntimeError("CLI down")
    assert is_trade_today(today8="20260904", cal=boom, log=lambda *_: None) is None


# ---------------- speculate：M_Final 纯计算 ----------------

import speculate  # noqa: E402


def _row(date, zt, up, dt, max_lb):
    """sent_rows 的行结构同 derive 派生的 sentiment.csv（date 为 ISO 横杠格式，
    _mf_env 内部与 _date_iso(date8) 做字符串比较）。"""
    return {"date": date, "index": 50.0, "label": "", "zt": zt, "dt": dt,
            "max_lb": max_lb, "promo_rate": 0.5, "up_ratio": up, "zhaban": 10}


def test_mf_env_aggressive():
    env, cur, prev = speculate._mf_env(
        [_row("2026-09-03", 80, 0.60, 10, 5), _row("2026-09-04", 80, 0.60, 10, 5)],
        "20260904")
    assert env == "aggressive" and prev == 5


def test_mf_env_aggressive_downgraded_on_height_collapse():
    # 昨日 ≥5 板且今日骤降 ≥2 → aggressive 降级 normal（高位崩塌闸门）
    env, _, _ = speculate._mf_env(
        [_row("2026-09-03", 80, 0.60, 10, 6), _row("2026-09-04", 80, 0.60, 10, 4)],
        "20260904")
    assert env == "normal"


def test_mf_env_freeze_and_defensive():
    env, _, _ = speculate._mf_env([_row("2026-09-04", 20, 0.25, 2, 2)], "20260904")
    assert env == "freeze"
    env, _, _ = speculate._mf_env([_row("2026-09-04", 40, 0.50, 5, 3)], "20260904")
    assert env == "defensive"          # 涨停 <45
    env, _, _ = speculate._mf_env([_row("2026-09-04", 80, 0.60, 35, 4)], "20260904")
    assert env == "defensive"          # 跌停 ≥30 恐慌闸门优先


def test_mf_env_empty_and_missing():
    assert speculate._mf_env([], "20260904")[0] == "normal"
    env, cur, _ = speculate._mf_env([_row("2026-09-04", None, None, 0, 1)], "20260904")
    assert env == "normal" and cur["zt"] is None


def test_pick_buckets_limit_up_group():
    f = {"group": "lu", "ft": "14:00", "seal_ratio": 0.05, "lb": 1,
         "tc": 2, "sealed": False, "amount": 1e9}
    assert set(speculate._pick_buckets(f)) == {
        "time_late", "seal_low", "ladder_1", "theme_2", "struct_reopen"}


def test_pick_buckets_nlu_group():
    f = {"group": "nlu", "ratio60": 0.94, "g20": 3, "volr": 0.8,
         "amount": 9e9, "sec": 0, "net": 0}
    assert set(speculate._pick_buckets(f)) == {
        "pos_high", "mom_low", "sector_none", "liq_big", "recog_none"}


def test_pick_buckets_clean_candidate():
    f = {"group": "lu", "ft": "09:35", "seal_ratio": 0.20, "lb": 3,
         "tc": 6, "sealed": True, "amount": 5e9}
    assert speculate._pick_buckets(f) == []
    assert speculate._pick_buckets(None) == []


@pytest.mark.parametrize("code6,expect", [
    ("600519", True), ("601398", True), ("603939", True), ("605111", True),
    ("000001", True), ("001979", True), ("002415", True), ("003816", True),
    ("300750", False), ("301236", False), ("688981", False), ("689009", False),
    ("830799", False), ("430047", False), ("920001", False),
])
def test_is_main_board(code6, expect):
    assert speculate._is_main_board(code6) is expect


def test_lu_candidate_negative_list_and_scoring():
    theme_count = {"机器人": 6}
    good = {"code": "600519", "name": "X", "is_st": False, "is_new": False,
            "成交额": 5e8, "封板资金": 1e8, "首次封板时间": "09:35",
            "连板数": 3, "涨停原因": "机器人+减速器", "最新价": 10.0,
            "最后封板时间": "09:35"}
    c = speculate._lu_candidate(good, theme_count, set(), {}, 55.0)
    assert c is not None and c["score"] >= 55 and c["theme"] == "机器人"
    assert c["factors"]["group"] == "lu"

    # 负面清单：尾盘板 / 成交 <1.5 亿 / 独狼板 / 昨日一字 / 非主板 / ST
    def variant(**kw):
        r = dict(good)
        r.update(kw)
        return r
    assert speculate._lu_candidate(
        variant(首次封板时间="14:50"), theme_count, set(), {}, 55.0) is None
    assert speculate._lu_candidate(
        variant(成交额=1e8), theme_count, set(), {}, 55.0) is None
    assert speculate._lu_candidate(
        variant(涨停原因="独立逻辑"), {"独立逻辑": 1}, set(), {}, 55.0) is None
    assert speculate._lu_candidate(
        good, theme_count, {"600519"}, {}, 55.0) is None      # 昨日一字排除
    assert speculate._lu_candidate(
        variant(code="300750"), theme_count, set(), {}, 55.0) is None  # 非主板
    assert speculate._lu_candidate(
        variant(is_st=True), theme_count, set(), {}, 55.0) is None


def test_nlu_candidate_hard_filters():
    base = {"thscode": "600000.SH", "name": "X", "close": 9.0, "high60": 10.0,
            "gain20": 10.0, "volr": 0.8, "ma10": 8.8, "ma20": 8.5, "ma20p": 8.3,
            "amount": 2e9, "amt5": 1e9, "pct": 1.0}
    c = speculate._nlu_candidate(base, {}, set(), {}, set(), {}, 55.0)
    assert c is not None and 0.86 <= base["close"] / base["high60"] <= 0.95

    def variant(**kw):
        m = dict(base)
        m.update(kw)
        return m
    # 甜区两端规避：追高 ≥0.95 / 深跌 <0.86
    assert speculate._nlu_candidate(variant(close=9.6), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(close=8.0), {}, set(), {}, set(), {}, 55.0) is None
    # 放量 / 20 日涨幅过热 / 成交不足 / ST / 非主板 / 已在涨停池
    assert speculate._nlu_candidate(variant(volr=1.5), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(gain20=45), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(amount=5e8, amt5=1e8), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(name="ST 某某"), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(thscode="300750.SZ"), {}, set(), {}, set(), {}, 55.0) is None
    assert speculate._nlu_candidate(variant(thscode="600000.SH"), {}, {"600000"}, set(), set(), {}, 55.0) is None


def test_pool_pick_row_shape_and_buy_zone():
    c = {"code": "600519", "name": "X", "lb": 3, "theme": "机器人", "score": 90,
         "price": 10.0, "role": "核心龙头", "nonzt": False, "factors": {"group": "lu"}}
    row = speculate._pool_pick_row(c)
    assert row["类型"] == "涨停板" and row["入选强度"] == 5
    assert row["明日触发条件"].startswith("开盘 0~+5%")       # 低开不接（M_Final）
    assert row["factors"] == {"group": "lu"}

    c2 = {"code": "600000", "name": "Y", "lb": 0, "theme": "银行", "score": 60,
          "price": None, "pct": 1.2, "net": 5e7, "role": "非涨停·缩量回调低吸",
          "nonzt": True, "factors": {"group": "nlu"}}
    row2 = speculate._pool_pick_row(c2)
    assert row2["类型"] == "非涨停板" and "龙虎榜净买入" in row2["核心逻辑"]
    assert row2["明日触发条件"].startswith("开盘 -2%~+3%")     # 开盘超窗放弃（M_Final）


# ---------------- http_retry：共享重试 ----------------

import http_retry  # noqa: E402


def test_retry_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(http_retry.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert http_retry.retry(flaky, tries=2, delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(http_retry.time, "sleep", lambda *_: None)

    def always():
        raise ValueError("down")

    with pytest.raises(ValueError):
        http_retry.retry(always, tries=2, delay=0)


# ---------------- derive 轮动统计：5日主线速度 / 持续强势（2026-09-04 重定义） ----------------

from derive import (_board_pcts, _jaccard_distance, _mainline_set,  # noqa: E402
                    _persistent_list)


def _mk_day(pcts):
    """{code: (名称, 涨幅)} → _board_pcts 输出形状。"""
    return {c: {"name": n, "pct": p} for c, (n, p) in pcts.items()}


def test_jaccard_distance_basics():
    assert _jaccard_distance({"a", "b"}, {"a", "b"}) == 0.0      # 原班不动
    assert _jaccard_distance({"a"}, {"b"}) == 1.0                # 完全换血
    assert _jaccard_distance({"a", "b"}, {"b", "c"}) == pytest.approx(2 / 3)
    assert _jaccard_distance(set(), set()) is None               # 双空=无信息，不假装 0/1


def test_mainline_set_ranks_by_cumulative_not_single_day():
    # A 每日 +1%（5日累计 +5.1%），B 首日 +4% 后连跌（累计 -4.1%）
    # → 5日窗口的主线是 A 而不是单日冲高但整体走弱的 B。
    days = []
    for d in range(5):
        days.append(_mk_day({"A": ("稳主线", 1.0),
                             "B": ("一日游", 4.0 if d == 0 else -2.0)}))
    top = _mainline_set(days, n=1)
    assert top == {"A"}


def test_persistent_list_qualifier_and_exclusions():
    # 14 个板块（10 个 +1% 的 filler 定出 Top10 门槛=跑赢 filler）、10 日窗口：
    # A：5 日 +3（在榜）、5 日 -1 → 在榜5日、累计+10.2%            → 入选
    # B：3 日 +3（在榜）、7 日 +0.9（低于 filler 掉榜）→ 在榜3日不足，虽累计 +16.3% → 剔除
    # E：4 日 +1.1（贴线在榜）、6 日 -1 → 在榜4日达标，但累计 -1.6% 不足 → 剔除
    pcts = []
    for d in range(10):
        day = {}
        day["A"] = ("波动主线", 3.0 if d % 2 == 0 else -1.0)      # 在榜日=0,2,4,6,8
        day["B"] = ("在榜不足", 3.0 if d in (1, 3, 5) else 0.9)
        day["E"] = ("累计不足", 1.1 if d < 4 else -1.0)
        for i in range(10):
            day[f"D{i}"] = (f"垫底{i}", 1.0)
        pcts.append(_mk_day(day))
    out = _persistent_list(pcts)
    names = {x["name"]: x for x in out}
    assert "波动主线" in names
    a = names["波动主线"]
    # 在榜日 = 0,2,4,6,8 共 5 日；累计 = 1.03^5 × 0.99^5 − 1 ≈ +10.2%
    assert a["days"] == 5 and a["cum"] == 10.2 and a["strong"] == 5 and a["window"] == 10
    assert "在榜不足" not in names      # 在榜 3/10 < 4（累计 +16.3% 达标也救不回）
    assert "累计不足" not in names      # 累计 -1.6% < 5%（在榜 4/10 达标也救不回）


def test_persistent_list_needs_min_window():
    assert _persistent_list([_mk_day({"A": ("a", 3.0)}) for _ in range(4)]) == []


def test_board_pcts_skips_invalid():
    day = {"boards": [{"code": "A", "name": "甲"}, {"code": "B", "name": "乙"}],
           "series": {"A": {"name": "甲 real", "pcts": [1.0, 2.5]},
                      "B": {"pcts": [None, "bad"]}}}
    out = _board_pcts(day)
    assert out == {"A": {"name": "甲 real", "pct": 2.5}}          # 取首个非法前的最后有效值口径=收盘


# ---------------- execution_layer：冻结执行层常量（2026-09-04 单一来源化） ----------------

from execution_layer import MF_BUY  # noqa: E402


def test_execution_layer_frozen_windows():
    """执行层冻结契约（C_Final = 最终筛选方案-主板概念.md 四.5/五.5）。"""
    assert MF_BUY["lu"] == {"win_min": 0.0, "win_max": 5.0, "low_min": -3.0,
                            "risk_low": -5.0, "risk_close": -4.0}
    assert MF_BUY["nlu"] == {"win_min": -2.0, "win_max": 3.0,
                             "risk_low": -4.0, "risk_close": -3.5}


def test_validator_and_pool_exec_share_execution_layer():
    """同一事实只建一处：验证器与竞价执行卡都引用 execution_layer.MF_BUY。"""
    import speculate  # noqa: E402
    import pool_exec  # noqa: E402
    assert speculate._MF_BUY is MF_BUY
    assert "execution_layer.MF_BUY" in open(pool_exec.__file__, encoding="utf-8").read()


# ---------------- pool_exec：昨日备选池 × 今日竞价执行判定 ----------------

from pool_exec import _verdict  # noqa: E402


def test_pool_exec_verdict_lu():
    assert _verdict("lu", None)[0] == "missing"
    assert _verdict("lu", -0.01)[1] == "低开·放弃"           # 低开不接
    assert _verdict("lu", 5.01)[1] == "高开超窗·放弃"        # 防情绪兑现
    assert _verdict("lu", 0.0)[0] == "ok"
    assert _verdict("lu", 5.0)[0] == "ok"                    # 窗口边界含


def test_pool_exec_verdict_nlu():
    assert _verdict("nlu", -2.01)[1] == "低开超窗·放弃"
    assert _verdict("nlu", 3.01)[0] == "skip"
    assert _verdict("nlu", -2.0)[0] == "ok"
    assert _verdict("nlu", 3.0)[0] == "ok"


def test_pool_exec_compute_rows_and_signal():
    import pool_exec as pe  # noqa: E402
    picks = [
        {"代码": "600000", "名称": "甲", "类型": "涨停板", "得分": 80, "所属概念": "X"},
        {"代码": "000001", "名称": "乙", "类型": "非涨停板", "得分": 70, "所属概念": "Y"},
    ]
    series = {"date": "2026-09-04", "rounds": [
        {"ts": "2026-09-04 09:15:30", "in_window": True, "items": [
            {"thscode": "600000.SH", "auction_pct": 2.0},
            {"thscode": "000001.SZ", "auction_pct": -3.0}]},
        {"ts": "2026-09-04 09:25:10", "in_window": True, "items": [
            {"thscode": "600000.SH", "auction_pct": 6.0},
            {"thscode": "000001.SZ", "auction_pct": -2.0}]},
    ]}
    d = pe.compute(picks, series, watchmap={"600000.SH": {"s": "L"}})
    assert d["ok"] and d["counts"] == {"ok": 1, "skip": 1, "missing": 0}
    assert d["rows"][0]["verdict"] == "高开超窗·放弃" and d["rows"][0]["pct"] == 6.0  # 末轮定盘值
    assert d["rows"][1]["verdict"] == "窗内·挂上穿"
    assert d["signal"]["lu_n"] == 1 and d["signal"]["open_down"] == 0.0


def test_pool_exec_clean_final_round_only():
    """可信定盘轮才并入判定（>09:26 抓取的 final 是现价快照，2026-09-04 实测被污染）。"""
    import pool_exec as pe  # noqa: E402
    series = {"date": "20260904", "rounds": [
        {"ts": "2026-09-04 09:19:30", "in_window": True,
         "items": [{"thscode": "600000.SH", "auction_pct": 2.0}]}]}
    pick = [{"代码": "600000", "名称": "甲", "类型": "涨停板"}]
    d = pe.compute(pick, series, {}, final_round=None)
    assert d["rows"][0]["pct"] == 2.0 and d["final"] is False
    clean = {"ts": "2026-09-04 09:25:10",
             "items": [{"thscode": "600000.SH", "auction_pct": 3.0}]}
    d2 = pe.compute(pick, series, {}, final_round=clean)
    assert d2["rows"][0]["pct"] == 3.0 and d2["final"] is True


# ---------------- speculate：样本外追踪 / 到期复审 / 观察项到期 ----------------

import speculate  # noqa: E402


def _cf_pick(close, triggered=True, sc=None, buckets=()):
    return {"close": close, "buy_triggered": triggered, "strong_concept": sc,
            "buckets": list(buckets)}


def test_oos_verdict_thresholds():
    f = speculate._oos_verdict
    assert f(59.75, 2.128, 10) == "ok"
    assert f(56.0, 2.0, 10) == "ok"        # 回落 3.75pct < 8
    assert f(51.0, 1.5, 10) == "warn"      # 胜率回落 ≥8pct
    assert f(59.0, 0.9, 10) == "warn"      # 均次 <1.0%
    assert f(47.0, 0.5, 10) == "severe"    # 胜率回落 ≥12pct
    assert f(55.0, -0.2, 10) == "severe"   # 均次 <0
    assert f(90.0, 5.0, 0) == "empty"


def test_oos_state_mixed_rules_and_cum():
    track = {"validations": {
        "20260907": {"rules": "cf", "picks": [_cf_pick(2.0), _cf_pick(-1.0, False, False, ["concept_cold"])]},
        "20260908": {"rules": "mf", "picks": [_cf_pick(9.0)]},   # 旧线不计入样本外
        "20260909": {"rules": "cf", "picks": [_cf_pick(1.0, sc=True)]},
    }}
    o = speculate._oos_state(track)
    assert o["days"] == 2 and o["n"] == 3 and o["since"] == "20260907"
    assert o["win_rate"] == round(2 / 3 * 100, 2)      # 赢单 = 2.0 与 1.0 两笔
    assert len(o["cum"]) == 2 and o["cum"][-1]["n"] == 3
    assert o["per_day"][0]["valid"] == 2
    assert "_wins" not in o["per_day"][0]              # 内部累加字段不下发


def test_observation_state_transitions():
    # concept_cold 到线且均次 ≤-0.4 → escalate；strong_concept 未到线 → observing
    rows = ([{"close": -0.5, "buckets": ["concept_cold"], "strong_concept": None}] * 30
            + [{"close": 1.0, "buckets": [], "strong_concept": True}] * 10)
    ob = speculate._observation_state(rows)
    assert ob["concept_cold"]["status"] == "escalate" and ob["concept_cold"]["n"] == 30
    assert ob["strong_concept"]["status"] == "observing"
    # strong_concept 到线且率 <30% → escalate；concept_cold 均次转正 → closed
    rows2 = ([{"close": 1.0, "buckets": [], "strong_concept": True}] * 5
             + [{"close": 1.0, "buckets": [], "strong_concept": False}] * 35
             + [{"close": 0.0, "buckets": ["concept_cold"]}] * 31)
    ob2 = speculate._observation_state(rows2)
    assert ob2["strong_concept"]["status"] == "escalate"
    assert ob2["concept_cold"]["status"] == "closed"


def test_val_attribute_concept_persistence_order():
    b = speculate._MF_BUY["nlu"]
    assert speculate._val_attribute("nlu", 0.5, 0.2, -1.0, 1.0, True, b,
                                    ind_pct=0.5, con_pct=-1.2) == "概念持续性不足"
    assert speculate._val_attribute("nlu", 0.5, 0.2, -1.0, 1.0, True, b,
                                    ind_pct=-1.0, con_pct=-1.2) == "板块持续性不足"  # 板块优先（引擎同序）
    assert speculate._val_attribute("nlu", 0.5, 0.2, -4.5, 1.0, True, b,
                                    ind_pct=0.5, con_pct=None) == "资金承接不足"      # 缺失→后续归因


# ---------------- backtest_capital：资金曲线执行口径（实时主口径无未来函数） ----------------

import os as _os  # noqa: E402
import sys as _sys  # noqa: E402

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_scripts = _os.path.join(_ROOT, "strategy-iter", "scripts")
if _scripts not in _sys.path:
    _sys.path.insert(0, _scripts)
import backtest_capital as bt  # noqa: E402


def _bt_row(grp, o, h, l, c, t1="2026-01-06", code="X"):
    return {"group": grp, "o": o, "h": h, "l": l, "c": c, "buy_triggered": False,
            "name": "名", "thscode": code, "T1": t1}


def test_backtest_realtime_lu_stop_and_skip():
    assert bt.trade_pnl(_bt_row("lu", 6.0, 7.0, 5.5, 6.5)) is None       # 高开 >5% 放弃
    assert bt.trade_pnl(_bt_row("lu", -1.0, 1.0, -1.5, 0.5)) is None     # 低开不接
    p = bt.trade_pnl(_bt_row("lu", 1.0, 1.2, -3.5, -2.0))                # 破 -3% → -3% 离场
    assert abs(p - ((1 - 0.03) / 1.01 - 1)) < 1e-9
    p2 = bt.trade_pnl(_bt_row("lu", 1.0, 4.0, 0.5, 3.0))                 # 未破位收盘离场
    assert abs(p2 - (1.03 / 1.01 - 1)) < 1e-9


def test_backtest_confirm_mode_is_lookahead_upper_bound():
    r = _bt_row("lu", 1.0, 1.2, -3.5, -2.0)
    assert bt.trade_pnl(r, mode="confirm") is None     # 回看：全天破 -3% 的直接不买
    r2 = _bt_row("lu", 1.0, 4.0, 0.5, 0.5)
    assert bt.trade_pnl(r2, mode="confirm") is None    # 回看：收盘未站上开盘不买
    assert bt.trade_pnl(r2) is not None                # 实时口径照样成交并承担亏损


def test_backtest_nlu_limit_fill_and_stop_priority():
    assert bt.trade_pnl(_bt_row("nlu", -1.5, -0.5, -2.0, -1.0)) is None  # 挂昨收未触及→未成交
    p = bt.trade_pnl(_bt_row("nlu", -1.5, 0.8, -0.5, 0.6))               # 触及成交→收盘
    assert abs(p - (1.006 / 1.0 - 1)) < 1e-9
    p2 = bt.trade_pnl(_bt_row("nlu", 1.0, 2.0, -4.5, 1.5))               # 风险位止损优先（保守）
    assert abs(p2 - (0.96 / 1.01 - 1)) < 1e-9


def test_backtest_curve_equal_weight_and_idle_cash():
    rows = [
        dict(_bt_row("lu", 0.0, 2.0, -0.5, 2.0), T1="2026-01-06", thscode="A"),
        dict(_bt_row("nlu", 1.0, 1.5, 0.5, 1.2), T1="2026-01-06", thscode="B"),
        dict(_bt_row("lu", -1.0, 0.0, -1.5, -1.0), T1="2026-01-07", thscode="C"),  # 低开放弃→空仓
    ]
    daily, eq, _dd = bt.build_curve(rows, mode="realtime")
    assert len(daily) == 2 and daily[0]["n_trade"] == 2
    pnl_a = 1.02 / 1.00 - 1
    pnl_b = 1.012 / 1.01 - 1
    assert abs(daily[0]["day_ret"] - (pnl_a + pnl_b) / 2) < 1e-9
    assert daily[1]["n_trade"] == 0 and daily[1]["day_ret"] == 0      # 未成交=空仓不亏
    assert abs(eq - (1 + daily[0]["day_ret"])) < 1e-9


# ---------------- gap_ahead_study：信号分桶（纯函数） ----------------

import gap_ahead_study as gs  # noqa: E402


def test_gap_study_bucket_table():
    days = [{"sig": {"med_gap": 0.2}, "out": {"day_ret": 1.0, "cond_avg": 2.0}},
            {"sig": {"med_gap": -0.8}, "out": {"day_ret": -1.0, "cond_avg": 0.5}},
            {"sig": {"med_gap": None}, "out": {"day_ret": 0.0, "cond_avg": 0.0}}]
    tb = gs.bucket_table(days, "med_gap", [-99, 0.0, 99], ["跌", "涨"])
    assert tb[0]["n"] == 1 and tb[0]["avg_ret"] == -1.0 and tb[0]["neg_days"] == 1
    assert tb[1]["n"] == 1 and tb[1]["avg_ret"] == 1.0 and tb[1]["neg_days"] == 0


# ---------------- ths_collect：轮动 daily 重建（定格点并入 + 节拍对齐） ----------------

_rot = _os.path.join(_ROOT, "backend", "rotation")
if _rot not in _sys.path:
    _sys.path.insert(0, _rot)
import ths_collect as tc  # noqa: E402


def _rot_raw(rounds):
    return {"date": "2026-09-05", "rounds": rounds}


def _rnd(t, w=True, px=10.0):
    return {"t": t, "w": w, "s": {"881101": [px, 1.0, 1000.0]}}


def test_rot_build_daily_appends_terminal_freeze():
    # 盘中循环 14:45 死亡、盘后任务补 15:00 定格（w=False）：并入作真实终态，不再整条丢弃
    raw = _rot_raw([_rnd("14:44"), _rnd("14:45"), _rnd("15:00", w=False, px=10.5)])
    out = tc.build_daily([("881101", "测试板块")], raw)
    assert out["times"][-1] == "15:00"
    assert out["coverage"]["last_time"] == "15:00"


def test_rot_build_daily_freeze_dedup_and_lunch():
    # 窗口内已有 15:00：定格不重复；午休 11:30 定格并入后时间轴仍有序
    raw = _rot_raw([_rnd("11:29"), _rnd("11:30", w=False, px=10.2),
                    _rnd("13:00"), _rnd("15:00")])
    out = tc.build_daily([("881101", "测试板块")], raw)
    assert out["times"].count("15:00") == 1
    assert "11:30" in out["times"] and out["times"] == sorted(out["times"])


def test_rot_build_daily_catchup_only_unchanged():
    # 全天窗口外（只补抓过一次）：维持「仅定格 1 点 + note」的原口径
    raw = _rot_raw([_rnd("15:00", w=False)])
    out = tc.build_daily([("881101", "测试板块")], raw)
    assert out["times"] == ["15:00"]
    assert "未逐轮采集" in (out["coverage"].get("note") or "")


def test_rot_next_tick_aligns_to_minute():
    from datetime import datetime as _dtm
    tick = tc._next_tick(_dtm(2026, 9, 5, 10, 0, 30, 500000), 60)
    assert (tick.second, tick.microsecond) == (2, 0) and tick.minute == 1
    assert (tick - _dtm(2026, 9, 5, 10, 0, 30, 500000)).total_seconds() >= 5
    tick2 = tc._next_tick(_dtm(2026, 9, 5, 10, 1, 1), 60)   # 压线 :01：退一个节拍
    assert (tick2.hour, tick2.minute, tick2.second) == (10, 2, 2)


if __name__ == "__main__":
    # 直跑入口：委托给 pytest（conftest 的路径引导已在上方先行生效）
    raise SystemExit(pytest.main([__file__, "-q"]))
