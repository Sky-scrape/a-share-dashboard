# -*- coding: utf-8 -*-
"""backend 纯函数单元测试（2026-09-04 新增）。

背景：此前全项目唯一测试是 tests/smoke.py 全链路冒烟 + 大量「读源码文本断言」
（能防函数被误删，不验行为）。这里对最值得保护的纯计算逻辑补真正的行为测试：
代码转换、交易日历口径、M_Final 环境分档/分桶、主板范围、备选池打分与买点文案、
共享重试。全部离线、不联网、不写数据目录。
"""
import os
import sys

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

import trade_cal  # noqa: E402
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


def test_trade_cal_default_path_falls_back_to_local(monkeypatch):
    """默认路径（真实 hithink）两级兜底：hithink 拿不到结论 → 退回本地日历。

    注入 cal 即单一来源（上方用例锁成员判定即结论）；这里锁默认路径的
    2026-10-02/05 假期断源修复：hithink 报错/陈旧未覆盖今日 → 本地日历覆盖今日
    且不在其中 = 权威非交易日；本地也不可用/未覆盖 → None（调用方按交易日继续，
    误判非交易日是静默失效）。桩掉 ht 与 _local_dates8：不触真实 CLI、
    不依赖真实 data/recap/us_market/ashare_cal.json。
    """
    import types

    def broken(*_a, **_k):
        raise RuntimeError("CLI down")

    monkeypatch.setitem(sys.modules, "ht", types.SimpleNamespace(ht=broken))
    monkeypatch.setattr(trade_cal, "_local_dates8", lambda: {"20260901", "20260905"})
    assert is_trade_today(today8="20260904", log=lambda *_: None) is False
    # hithink 陈旧（未覆盖今日）同样退本地，不把「不在其中」当非交易日（08-31 事故）
    monkeypatch.setitem(sys.modules, "ht", types.SimpleNamespace(
        ht=lambda *_a, **_k: {"item": [{"date": "20260102"}, {"date": "20260105"}]}))
    monkeypatch.setattr(trade_cal, "_local_dates8", lambda: {"20260901", "20260904"})
    assert is_trade_today(today8="20260904", log=lambda *_: None) is True
    # 本地未覆盖今日 / 本地也不可用 → None（按交易日继续）
    monkeypatch.setattr(trade_cal, "_local_dates8", lambda: {"20260901", "20260903"})
    assert is_trade_today(today8="20260904", log=lambda *_: None) is None
    monkeypatch.setattr(trade_cal, "_local_dates8", lambda: None)
    assert is_trade_today(today8="20260904", log=lambda *_: None) is None


# ---------------- watchdog：复盘快照新鲜度判定（10-15 缺口事故防回退） ----------------
# 10-15 抓取链被系统级中断后静默消失（无 traceback/无 fetch end/快照未落盘），
# bat 尾部失败推送没机会执行；看门狗是唯一独立于采集链的常驻探针，故其判定值得锁。

import watchdog  # noqa: E402
from watchdog import _recap_freshness_missing as _recap_missing  # noqa: E402

# 含周末缺口（10-17/18 为非交易日）的日历样本：日历覆盖今日但今日不在其中
_CAL = {"20261012", "20261013", "20261014", "20261015",
        "20261016", "20261019", "20261020"}


def _mk_snap(d, *dates):
    for x in dates:
        (d / f"{x}.json").write_text("{}", encoding="utf-8")


def test_watchdog_recap_ok_when_snapshot_present(tmp_path):
    _mk_snap(tmp_path, "20261015")
    assert _recap_missing("20261016", (10, 0), _CAL, str(tmp_path)) is False


def test_watchdog_recap_before_deadline_ignores_today(tmp_path):
    # 17:05 任务窗口内（今日未到截止时刻，今日不算候选）：上一个交易日有快照即无告警
    _mk_snap(tmp_path, "20261015")
    assert _recap_missing("20261016", (17, 10), _CAL, str(tmp_path)) is False


def test_watchdog_recap_alerts_after_deadline_when_today_missing(tmp_path):
    _mk_snap(tmp_path, "20261015")
    assert _recap_missing("20261016", (18, 30), _CAL, str(tmp_path)) is True


def test_watchdog_recap_alerts_for_prev_trade_day_gap(tmp_path):
    # 当晚关机/次日才开机：缺口要在下一轮巡检（次日盘前）就被看到
    _mk_snap(tmp_path, "20261014")
    assert _recap_missing("20261016", (9, 0), _CAL, str(tmp_path)) is True


def test_watchdog_recap_gz_snapshot_counts(tmp_path):
    (tmp_path / "20261015.json.gz").write_text("", encoding="utf-8")
    assert _recap_missing("20261016", (10, 0), _CAL, str(tmp_path)) is False


def test_watchdog_recap_non_trade_day_only_requires_prev(tmp_path):
    # 非交易日（10-17 周六）自身不需要快照，只看上一个交易日（10-16）
    _mk_snap(tmp_path, "20261016")
    assert _recap_missing("20261017", (10, 0), _CAL, str(tmp_path)) is False
    assert _recap_missing("20261017", (10, 0), _CAL, str(tmp_path / "nope")) is True


def test_watchdog_recap_calendar_unusable_is_silent(tmp_path):
    # 日历陈旧未覆盖今日 / 日历不可用 → 不下结论（宁可漏报也不误报）
    assert _recap_missing("20270101", (10, 0), _CAL, str(tmp_path)) is False
    assert _recap_missing("20261016", (18, 30), None, str(tmp_path)) is False


def test_watchdog_recap_alert_swallows_notify_failure(monkeypatch):
    """告警链路炸掉不得反噬探活。

    _recap_alert 在 run_once 的两个分支之前调用：一旦抛出，watchdog.json 连
    「服务失联连败计数」一起丢，30 分钟阈值永远到不了。历史事故形状是裸 python
    解到 LibreOffice 解释器、`import notify`（requests）直接炸——静默失效数天。
    """
    import types

    monkeypatch.setattr(watchdog, "_recap_freshness_missing", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "_log", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(
        send=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))))
    st = {}
    watchdog._recap_alert(st)                    # 不抛（探活优先级高于告警）
    assert st.get("recap_alert_date") is None    # 未标记成功，下一轮还会再试


# ---------------- fetch_daily：整轮抓取硬上限（挂死兜底，10-15 缺口事故防回退） ----------------

def test_fetch_deadline_hard_exits_stuck_run(tmp_path):
    """到点必须真的终止进程，并顺手释放抓取锁。

    挂死的线程阻塞在内核 socket 上、无法合作式取消，唯一可靠的中断是 os._exit——
    只能起子进程断言（os._exit 会带走整个进程）。锁不显式释放的话，下一轮要等
    40 分钟过期接管才放行，正是「挂死一整晚」要避免的形状。
    """
    import subprocess as _sp

    recap_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "backend", "recap")
    lock = tmp_path / "fetch-recap.lock"
    # 子进程走产品自身的 lockutil.acquire 拿锁：pid 即子进程 pid（release 只删
    # 「本进程持有」的锁，这是真实运行时的一致形状，也免去在测试里手写文件写入）
    code = (
        "import sys, time;"
        "sys.path.insert(0, %r);"
        "import fetch_daily as f, lockutil, logutil;"
        "f.LOCK_PATH = %r;"
        "assert lockutil.acquire(f.LOCK_PATH);"
        "f._arm_deadline(0.3, logutil.get_logger('t'));"
        "time.sleep(5);"
        "print('NOT-KILLED')"
    ) % (recap_dir, str(lock))
    r = _sp.run([sys.executable, "-c", code], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 1, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert "NOT-KILLED" not in r.stdout          # 没跑到 sleep 之后
    assert "硬上限" in r.stdout                   # 留了一条可诊断日志
    assert not lock.exists()                      # 锁已释放，不挡下一轮


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


# ---------------- fetch_global：A股热力图数据日口径（2026-09-18） ----------------

def _load_fetch_global():
    """隔离加载 fetch_global（其模块级 stdout 包装不应污染测试进程捕获）。

    asof 口径背景：live snapshot 行不带日期字段，曾误用行业映射 vintage（成分映射
    建库日，滞后行情数日）当数据日。现取「轮动日线最近交易日」，map vintage 降为
    附属标注。"""
    import importlib.util
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "backend", "global", "fetch_global.py")
    # 模块级会执行 sys.stdout = TextIOWrapper(sys.stdout.buffer, ...)：pytest 捕获流
    # 在用例结束后关闭，留下的包装句柄会让解释器收尾报 lost sys.stderr——
    # 用一次性哑流接住这次包装，真实 stdout/stderr 全程不动。
    dumb_out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    dumb_err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = dumb_out, dumb_err
    try:
        spec = importlib.util.spec_from_file_location("fetch_global_under_test", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    return mod


def test_cn_snapshot_date_prefers_rotation_daily(tmp_path):
    mod = _load_fetch_global()
    d = tmp_path / "daily"
    d.mkdir()
    (d / "2026-09-10.json").write_text("{}", encoding="utf-8")
    (d / "20260908.json").write_text("{}", encoding="utf-8")     # 老命名兼容
    (d / "2026-09-12.json").write_text("{}", encoding="utf-8")
    (d / "notadate.json").write_text("{}", encoding="utf-8")     # 无日期文件必须忽略
    assert mod._cn_snapshot_date8(daily_dir=str(d)) == "20260912"


def test_cn_snapshot_date_falls_back_to_board_daily(tmp_path):
    mod = _load_fetch_global()
    empty = tmp_path / "none"
    empty.mkdir()
    bd = {"种植业与林业": {"2026-09-10": 1.0, "2026-09-11": -0.5}}
    assert mod._cn_snapshot_date8(bd, daily_dir=str(empty)) == "20260911"


def test_cn_snapshot_date_missing_everywhere_returns_empty(tmp_path):
    mod = _load_fetch_global()
    empty = tmp_path / "none"
    empty.mkdir()
    assert mod._cn_snapshot_date8({}, daily_dir=str(empty)) == ""


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


def test_backtest_proxy_constants_come_from_execution_layer():
    """口径对齐（2026-09-10）：strategy-iter 可执行口径引用执行层单一来源、不得自带副本。

    历史上 backtest_capital 手抄了一份常量，并把「买入触发过滤」low_min=-3% 误当出场
    止损（执行层风险位实为 risk_low=-5% / risk_close=-4%）；本断言锁死该口径不再漂移。
    """
    import importlib.util
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "strategy-iter", "scripts", "backtest_capital.py")
    spec = importlib.util.spec_from_file_location("bt_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.LU == MF_BUY["lu"]
    assert mod.NLU == MF_BUY["nlu"]


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


def _c7_pick(close, triggered=True, sc=None, buckets=()):
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
        "20260907": {"rules": "c7", "picks": [_c7_pick(2.0), _c7_pick(-1.0, False, False, ["concept_cold"])]},
        "20260908": {"rules": "mf", "picks": [_c7_pick(9.0)]},   # 旧线不计入样本外
        "20260909": {"rules": "c7", "picks": [_c7_pick(1.0, sc=True)]},
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


def test_speculate_facade_reexports_are_single_source():
    """门面必须纯转发（2026-09-11 补锁）：口径常量与子模块定义是同一对象。

    smoke 的「七文件拼接源码 in」断言检测不出重复副本——门面与子模块各留一份
    口径常量时照样通过；这里用身份断言堵住该盲区。"""
    import spec_rules, spec_series, spec_duckdb   # noqa: E402
    import spec_builders, spec_pool, spec_validate   # noqa: E402
    for mod, names in (
        (spec_rules, ("INDEX_BENCH", "NEAR_D10", "NEAR_D30", "NORMAL_D3", "RULES",
                      "SCAN_TH", "SEVERE_D10", "SEVERE_D30")),
        (spec_series, ("PANEL_SENTIMENT", "index_gain")),
        (spec_duckdb, ("CACHE_DIR", "scan_deviation")),
        (spec_builders, ("_FATE_ORDER", "build_themes")),
        (spec_pool, ("_CONCEPT_LU_TIERS", "_MF_QUOTA", "RULES_VERSION", "build_pool")),
        (spec_validate, ("_MF_BUY", "_OPT_MIN_SCORE", "_OOS_BASELINE",
                         "_OOS_REVIEW_EVERY", "POOL_TRACK", "POOL_OPT")),
    ):
        for name in names:
            assert getattr(speculate, name) is getattr(mod, name), f"{mod.__name__}.{name}"


# ---------------- us_market：隔夜美股场次收盘判定（终版池前置守卫） ----------------

import us_market   # noqa: E402
from datetime import datetime as _dtm, timezone as _tzc   # noqa: E402


def test_session_closed_follows_us_east_1615():
    """场次收盘 = 美东 16:15（北京 04:15 夏令 / 05:15 冬令），与抓取层 _bar_closed 同规则。

    旧版用「updated ≥ T+1 03:00」墙钟：冬令 03:00–05:15 间的重建会把未收盘场次
    误判为终版。这里用固定美东时刻锁死三个窗口（盘中 / 收盘前一刻 / 收盘后）。"""
    summer, winter = "2026-07-08", "2026-01-08"   # 两边都在各自时令段内
    # 盘中：夏令美东 15:00（=UTC 19:00）/ 冬令美东 14:00（=UTC 19:00）→ 未收盘
    assert us_market._session_closed(summer, _dtm(2026, 7, 8, 19, 0, tzinfo=_tzc.utc)) is False
    assert us_market._session_closed(winter, _dtm(2026, 1, 8, 19, 0, tzinfo=_tzc.utc)) is False
    # 收盘后：夏令美东 18:00（=UTC 22:00）/ 冬令美东 17:00（=UTC 22:00）→ 已收盘
    assert us_market._session_closed(summer, _dtm(2026, 7, 8, 22, 0, tzinfo=_tzc.utc)) is True
    assert us_market._session_closed(winter, _dtm(2026, 1, 8, 22, 0, tzinfo=_tzc.utc)) is True
    # 临界点：美东 16:15 整即视为收盘（与 _bar_closed 的 now >= close_dt 一致）
    assert us_market._session_closed(summer, _dtm(2026, 7, 8, 20, 15, tzinfo=_tzc.utc)) is True
    assert us_market._session_closed(summer, _dtm(2026, 7, 8, 20, 14, tzinfo=_tzc.utc)) is False


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
    """实时主口径（2026-09-10 口径对齐）：涨停组风险位 = 执行层 risk_low=-5%。

    对齐前本脚本误用买入触发过滤 low_min=-3% 作出场；-3% 破位但未破 -5% 的样本
    现应持有到收盘（下例 p3）。
    """
    assert bt.trade_pnl(_bt_row("lu", 6.0, 7.0, 5.5, 6.5)) is None       # 高开 >5% 放弃
    assert bt.trade_pnl(_bt_row("lu", -1.0, 1.0, -1.5, 0.5)) is None     # 低开不接
    p = bt.trade_pnl(_bt_row("lu", 1.0, 1.2, -5.5, -2.0))                # 破 risk_low -5% → -5% 离场
    assert abs(p - ((1 - 0.05) / 1.01 - 1)) < 1e-9
    p3 = bt.trade_pnl(_bt_row("lu", 1.0, 1.2, -3.5, -2.0))               # 破 -3% 未破 -5%：收盘离场
    assert abs(p3 - ((1 - 0.02) / 1.01 - 1)) < 1e-9
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


# ---------------- fsutil：原子写盘单一来源（2026-09-10 收敛） ----------------

import gzip as _gzip   # noqa: E402
import json as _json   # noqa: E402

import fsutil   # noqa: E402


def test_fsutil_json_gzip_sig_roundtrip(tmp_path):
    p = tmp_path / "a.json"
    fsutil.save_json_atomic(p, {"名": "值", "n": 1}, indent=2)
    assert _json.loads(p.read_text(encoding="utf-8")) == {"名": "值", "n": 1}
    # default/sort_keys 透传（run_job 落盘 numpy 标量、manifest 排序键依赖）
    d = tmp_path / "d.json"
    fsutil.save_json_atomic(d, {"b": 1, "a": object()}, default=str, sort_keys=True)
    assert list(_json.loads(d.read_text(encoding="utf-8"))) == ["a", "b"]
    # utf-8-sig：CSV 场景 BOM 保持
    csvp = tmp_path / "b.csv"
    fsutil.save_text_atomic(csvp, "x,y\r\n1,2\r\n", encoding="utf-8-sig")
    assert csvp.read_bytes()[:3] == b"\xef\xbb\xbf"
    # gzip JSON 往返（快照归档路径）
    gzp = tmp_path / "c.json.gz"
    fsutil.save_gzip_json_atomic(gzp, {"k": [1, 2]})
    assert _json.loads(_gzip.decompress(gzp.read_bytes()).decode("utf-8")) == {"k": [1, 2]}


def test_fsutil_newline_param_and_tmp_cleanup(tmp_path):
    # newline="\n" 强制 LF（recap 笔记口径）；缺省与 Path.write_text 同（平台翻译）
    lf = tmp_path / "lf.md"
    fsutil.save_text_atomic(lf, "a\nb\n", newline="\n")
    assert lf.read_bytes() == b"a\nb\n"
    native = tmp_path / "native.txt"
    fsutil.save_text_atomic(native, "a\nb\n")
    assert native.read_bytes() == (b"a\r\nb\r\n" if _os.name == "nt" else b"a\nb\n")
    # 写入中途异常（不可编码字符）→ 临时文件被清理、目标不出现
    with pytest.raises(UnicodeEncodeError):
        fsutil.save_text_atomic(tmp_path / "bad.txt", "中文\ud800surrogate")
    # iterdir() 顺序文件系统相关（NTFS 创建序 / tmpfs 另序），按名字集合断言
    assert {p.name for p in tmp_path.iterdir()} == {"lf.md", "native.txt"}




# ---------------- 日线层备源兜底与研究库 stale 探测（2026-09-15） ----------------

def test_spec_ohlc_fb_symbol_mapping():
    import spec_ohlc_fb
    assert spec_ohlc_fb._symbol_of("600601") == "sh600601"
    assert spec_ohlc_fb._symbol_of("605258") == "sh605258"
    assert spec_ohlc_fb._symbol_of("000823") == "sz000823"
    assert spec_ohlc_fb._symbol_of("300750") == "sz300750"
    assert spec_ohlc_fb._symbol_of("920819") is None   # 北交所不在池口径，不猜前缀
    assert spec_ohlc_fb._symbol_of("") is None
    assert spec_ohlc_fb._symbol_of("abc123") is None


def test_spec_ohlc_fb_ohlc_from_bars_matches_duckdb_math():
    # 与 spec_duckdb.ohlc_rets 同构：涨幅相对前一根 bar 收盘（LAG 语义）；
    # 量比 amtr 为估计口径（量比×价比，腾讯日线无成交额字段）
    import spec_ohlc_fb
    bars = [{"date": "2026-09-11", "open": 31.39, "close": 34.77, "high": 34.77,
             "low": 31.05, "volume": 80647.0},
            {"date": "2026-09-14", "open": 34.50, "close": 35.06, "high": 36.66,
             "low": 34.10, "volume": 127505.0},
            {"date": "2026-09-15", "open": 34.63, "close": 36.16, "high": 36.96,
             "low": 34.60, "volume": 97286.0}]   # 盘中未收盘 bar：不得参与定位
    r = spec_ohlc_fb.ohlc_from_bars("2026-09-14", bars)
    assert r == {"o": round((34.50 / 34.77 - 1) * 100, 2),
                 "h": round((36.66 / 34.77 - 1) * 100, 2),
                 "l": round((34.10 / 34.77 - 1) * 100, 2),
                 "c": round((35.06 / 34.77 - 1) * 100, 2),
                 "amtr": round((127505.0 / 80647.0) * (35.06 / 34.77), 2),
                 "src": "em"}
    assert spec_ohlc_fb.ohlc_from_bars("2026-09-14", bars[::-1]) == r   # 乱序输入可换算
    assert spec_ohlc_fb.ohlc_from_bars("2026-09-16", bars) is None      # 缺 T 日 bar
    assert spec_ohlc_fb.ohlc_from_bars("2026-09-11", bars) is None      # 缺前收（真停牌如实缺席）
    zero_vol = [bars[0], dict(bars[1], volume=0.0)]
    assert spec_ohlc_fb.ohlc_from_bars("2026-09-14", zero_vol)["amtr"] is None


def test_ohlc_rets_fallback_merge_and_status(monkeypatch, tmp_path):
    # 缺口日：库内缺 000823 → 东财备源补齐 + 状态落盘 ok=False + 备源计数
    import json as _json
    import spec_duckdb, spec_ohlc_fb

    def fake_export(sql, tag):
        if tag.startswith("poolval"):
            return [{"thscode": "600601.SH", "o": 1.0, "h": 2.0, "l": -1.0,
                     "c": 1.5, "amtr": 1.1}]
        if tag.startswith("layerprobe"):
            return [{"n": 0, "last_bar": "2026-09-11"}]
        return []

    monkeypatch.setattr(spec_duckdb, "_db_export", fake_export)
    monkeypatch.setattr(spec_ohlc_fb, "day_rets",
                        lambda d, cs: {c: {"o": 0.1, "h": 0.2, "l": 0.0, "c": 0.15,
                                           "amtr": 1.0, "src": "em"} for c in cs})
    monkeypatch.setattr(spec_duckdb, "DUCKDB_STATUS", str(tmp_path / "duckdb.json"))
    spec_duckdb._PROBE_CACHE.clear()
    out = spec_duckdb.ohlc_rets("20260914", ["600601", "000823"])
    assert out["600601"]["c"] == 1.5 and "src" not in out["600601"]
    assert out["000823"]["src"] == "em"
    st = _json.loads((tmp_path / "duckdb.json").read_text(encoding="utf-8"))
    assert st["ok"] is False and st["layer_n"] == 0 and st["last_bar"] == "2026-09-11"
    assert st["fallback"] == {"n": 1, "ok": 1, "codes": ["000823"]}
    assert spec_duckdb.layer_status()["probed"] == "20260914"


def test_ohlc_rets_healthy_day_no_probe_no_alarm(monkeypatch, tmp_path):
    # 齐全日：不触发层探测（省一次 CLI 调用），状态 ok=True、last_bar=验证日下界
    import json as _json
    import spec_duckdb

    calls = []

    def fake_export(sql, tag):
        calls.append(tag)
        return [{"thscode": "600601.SH", "o": 1.0, "h": 2.0, "l": -1.0,
                 "c": 1.5, "amtr": 1.1}]

    monkeypatch.setattr(spec_duckdb, "_db_export", fake_export)
    monkeypatch.setattr(spec_duckdb, "DUCKDB_STATUS", str(tmp_path / "duckdb.json"))
    spec_duckdb._PROBE_CACHE.clear()
    out = spec_duckdb.ohlc_rets("20260914", ["600601"])
    assert out["600601"]["c"] == 1.5
    assert [t for t in calls if not t.startswith("poolval")] == []   # 无额外探测调用
    st = _json.loads((tmp_path / "duckdb.json").read_text(encoding="utf-8"))
    assert st["ok"] is True and st["last_bar"] == "2026-09-14" and st["fallback"] is None


def test_market_amt_ratio_requires_t_day_bar(monkeypatch):
    # 本地库落后（T 日 bar 缺失）时必须返回 None：拿 T-1 当 T 是静默用错日数据
    import spec_duckdb
    stale_rows = [{"date": "2026-09-11", "amt": 1.0}] + [
        {"date": f"2026-09-{d:02d}", "amt": 1.0} for d in range(1, 11)]
    monkeypatch.setattr(spec_duckdb, "_db_export", lambda sql, tag: stale_rows)
    assert spec_duckdb._market_amt_ratio("20260914") is None
    fresh_rows = [{"date": "2026-09-14", "amt": 2.0}] + [
        {"date": f"2026-09-{d:02d}", "amt": 1.0} for d in range(1, 20)]
    monkeypatch.setattr(spec_duckdb, "_db_export", lambda sql, tag: fresh_rows)
    assert spec_duckdb._market_amt_ratio("20260914") == 2.0


def test_validate_prev_pool_marks_em_source_and_note(monkeypatch):
    # 备源回填的行带 data_src="em"，payload note 说明缺口与回填数量
    import spec_validate

    snap = {"modules": {"speculation": {"data": {"pool": {
        "picks": [{"代码": "605258", "名称": "协和电子", "类型": "涨停板",
                   "得分": 78.5, "factors": {"group": "lu", "lb": 1, "sealed": True}}],
        "note": "C7 主板概念口径", "env": "defensive"}}}}}
    monkeypatch.setattr(spec_validate.snapio, "load", lambda d8: snap)
    monkeypatch.setattr(spec_validate.snapio, "list_dates", lambda: ["20260914", "20260911"])
    monkeypatch.setattr(spec_validate, "_trade_days", lambda: {"20260911"})  # 受控日历
    monkeypatch.setattr(spec_validate, "ohlc_rets",
                        lambda d8, cs: {"605258": {"o": -0.78, "h": 5.44, "l": -1.93,
                                                   "c": 0.83, "amtr": 1.63, "src": "em"}})
    monkeypatch.setattr(spec_validate, "layer_status",
                        lambda: {"probed": "20260914", "ok": False, "layer_n": 0,
                                 "last_bar": "2026-09-11"})
    monkeypatch.setattr(spec_validate, "index_series", lambda *a: [])
    monkeypatch.setattr(spec_validate, "_industry_lookup", lambda: {})
    val = spec_validate.validate_prev_pool("20260914", [{"date": "2026-09-11"}])
    assert val["ok"] and val["picks"][0]["data_src"] == "em"
    assert "全市场日线" in (val["note"] or "") and "备源" in (val["note"] or "")
    # 台账留痕：日历来源两侧都写明（2026-10-16 日期匹配修正）
    assert val["calendar"]["source"] == "ashare_cal"
    assert val["calendar"]["prev"] == "2026-09-11"
    assert val["calendar"]["panel_prev"] == "2026-09-11"


# ---------------- 上一交易日解析：面板缺行不得回退到更早的池（C15 复审修正） --------

def test_prev_pool_date_uses_authoritative_calendar():
    """一级来源：权威 A 股日历（跨节假日自动对齐，不靠面板/快照存在性）。"""
    import spec_validate as sv
    cal = {"20260924", "20260928", "20260929", "20261008"}
    # 节假日（09-25 中秋）后重开：09-28 的上一交易日是 09-24，不是「前一天」
    d8, meta = sv._prev_pool_date("20260928", [], [], trade_days=cal)
    assert d8 == "20260924" and meta["source"] == "ashare_cal"
    # 09-29 的上一个交易日是 09-28（旧实现因面板缺行错验成 09-24 的那一天）
    assert sv._prev_pool_date("20260929", [], [], trade_days=cal)[0] == "20260928"
    # 日历含未来日期（ashare_cal 覆盖到年末）不得入选
    assert sv._prev_pool_date("20260929", [], [], trade_days=cal | {"20261010"})[0] == "20260928"
    # 日历里无更早交易日 → None（调用方不验证，绝不编造）
    assert sv._prev_pool_date("20260924", [], [], trade_days=cal)[0] is None


def test_prev_pool_date_falls_back_to_panel_and_snapshots():
    """二级来源（日历不可用）：面板 ∪ 快照取最新，仍不回退到更早的池。"""
    import spec_validate as sv
    # 面板缺 09-14（0928 场景复刻）→ 快照补上
    d8, meta = sv._prev_pool_date("20260915", [{"date": "2026-09-11"}],
                                  ["20260914", "20260911"], trade_days=set())
    assert d8 == "20260914" and meta["source"] == "panel+snap"
    assert meta["panel_prev"] == "2026-09-11" and meta["snap_prev"] == "2026-09-14"
    # 面板比快照新 → 取面板
    d8, meta = sv._prev_pool_date("20260915", [{"date": "2026-09-14"}], ["20260911"],
                                  trade_days=set())
    assert d8 == "20260914" and meta["snap_prev"] == "2026-09-11"
    # 两侧都为空 → None
    assert sv._prev_pool_date("20260915", [], [], trade_days=set())[0] is None
    # 验证日当天/之后的日期不得入选
    assert sv._prev_pool_date("20260915", [{"date": "2026-09-15"}], ["20260915"],
                              trade_days=set())[0] is None


def test_validate_prev_pool_fails_closed_when_snapshot_missing(monkeypatch):
    """上一交易日快照缺失时留空待补，绝不拿更早的池顶替（错配样本源头）。"""
    import spec_validate as sv
    monkeypatch.setattr(sv.snapio, "list_dates", lambda: ["20260924", "20260923"])
    # 日历精确给出 09-28（该日快照缺失）→ fail-closed，不得回退到 09-24
    monkeypatch.setattr(sv, "_trade_days", lambda: {"20260924", "20260923", "20260928"})
    monkeypatch.setattr(sv.snapio, "load", lambda d8: pytest.fail("不应读取更早的池"))
    val = sv.validate_prev_pool("20260929", [{"date": "2026-09-28"}])
    assert val["ok"] is False and val["pending"] is True
    assert val["date"] == "20260928" and "留空待补" in val["note"]
    assert "不回退" in val["note"]


def test_validate_prev_pool_uses_snapshot_when_panel_lags(monkeypatch):
    """0929 场景：面板缺 09-28 行，快照有 → 验证 09-28 池（旧实现会错验 09-24）。"""
    import spec_validate as sv
    seen = {}
    snap = {"modules": {"speculation": {"data": {"pool": {
        "picks": [{"代码": "600802", "名称": "福建水泥", "类型": "涨停板", "得分": 84.0}],
        "note": "C7 主板概念口径", "env": "defensive"}}}}}

    def _load(d8):
        seen["d8"] = d8
        return snap
    monkeypatch.setattr(sv.snapio, "list_dates", lambda: ["20260928", "20260924"])
    monkeypatch.setattr(sv, "_trade_days", lambda: None)   # 日历不可用 → 走兜底来源
    monkeypatch.setattr(sv.snapio, "load", _load)
    monkeypatch.setattr(sv, "ohlc_rets", lambda d8, cs: {"600802": {"o": 0.6, "h": 9.97,
                                                                   "l": 0.0, "c": 9.97}})
    monkeypatch.setattr(sv, "layer_status", lambda: {"layer_n": 1, "last_bar": "2026-09-29"})
    monkeypatch.setattr(sv, "index_series", lambda *a: [])
    monkeypatch.setattr(sv, "_industry_lookup", lambda: {})
    val = sv.validate_prev_pool("20260929", [{"date": "2026-09-24"}])
    assert seen["d8"] == "20260928" and val["date"] == "20260928"
    assert val["calendar"]["source"] == "panel+snap"
    assert val["calendar"]["snap_prev"] == "2026-09-28"


# ------------------------------------------------- 出站守卫入口（O-1 · 2026-09-27）

def test_guarded_get_allows_and_blocks(monkeypatch):
    """guarded_get：白名单内放行到 requests；白名单外/http 明文在守卫层挡下。"""
    import netguard
    import http_retry
    calls = []
    monkeypatch.setattr(http_retry.requests, "get", lambda url, **k: calls.append(url))
    monkeypatch.setattr(netguard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    hosts = ("push2his.eastmoney.com",)
    http_retry.guarded_get("https://push2his.eastmoney.com/api", allow_hosts=hosts)
    assert calls and calls[0].startswith("https://push2his")
    with pytest.raises(ValueError):   # 白名单外主机
        http_retry.guarded_get("https://evil.example.com/api", allow_hosts=hosts)
    with pytest.raises(ValueError):   # http 明文（守卫要求 https）
        http_retry.guarded_get("http://push2his.eastmoney.com/api", allow_hosts=hosts)
    assert len(calls) == 1            # 两次拦截都未到达 requests


# ------------------------------------------------- 上游失败现场采样（2026-10-27）

class _FakeResp:
    """最小 Response 桩：safe_get / capture_sample 只摸 status_code 与 content。"""

    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


def test_capture_sample_writes_status_body_and_prunes(tmp_path, monkeypatch):
    """采样落盘：状态码 + body 头写入文件；目录按 keep 滚动只留最新若干份。"""
    import netguard
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path))
    for _ in range(3):
        netguard.capture_sample("https://h.example.com/api",
                                resp=_FakeResp(503, b"<html>busy</html>"))
    assert len(os.listdir(tmp_path)) == 3
    netguard.capture_sample("https://h.example.com/api",
                            resp=_FakeResp(503, b"<html>busy</html>"), keep=2)
    files = sorted(os.listdir(tmp_path))
    assert len(files) == 2                       # 滚动只留最新 keep 份
    text = (tmp_path / files[-1]).read_text(encoding="utf-8")
    assert "status: 503" in text and "<html>busy</html>" in text
    assert "url: https://h.example.com/api" in text


def test_capture_sample_records_exception_and_never_raises(tmp_path, monkeypatch):
    """无 resp/exc 不采样；异常路径记异常类型；采样自身失败绝不外抛。"""
    import netguard
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path))
    assert netguard.capture_sample("https://h.example.com/x") is None
    p = netguard.capture_sample("https://h.example.com/x", exc=TimeoutError("timed out"))
    assert p and os.path.exists(p)
    assert "error: TimeoutError: timed out" in open(p, encoding="utf-8").read()
    # 目录不可写（换成文件路径冒充目录）→ 返回 None 而非抛
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path / "f"))
    (tmp_path / "f").write_text("not a dir", encoding="utf-8")
    assert netguard.capture_sample("https://h.example.com/x",
                                   exc=OSError("down")) is None


def test_safe_get_captures_upstream_failures(tmp_path, monkeypatch):
    """safe_get 失败分支留现场：非 200 采状态码+body、网络异常采异常、200 零采样。"""
    import netguard
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path))
    monkeypatch.setattr(netguard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    hosts = ("web.ifzq.gtimg.cn",)

    def _boom(url, **k):
        raise TimeoutError("t")

    monkeypatch.setattr(netguard.requests, "get", _boom)
    with pytest.raises(TimeoutError):
        netguard.safe_get("https://web.ifzq.gtimg.cn/api", hosts)
    monkeypatch.setattr(netguard.requests, "get", lambda url, **k: _FakeResp(502, b"gw"))
    netguard.safe_get("https://web.ifzq.gtimg.cn/api", hosts)
    monkeypatch.setattr(netguard.requests, "get", lambda url, **k: _FakeResp())
    netguard.safe_get("https://web.ifzq.gtimg.cn/api", hosts)
    assert len(os.listdir(tmp_path)) == 2        # 200 正常响应不采样


def test_latest_sample_note_reports_newest_or_none(tmp_path, monkeypatch):
    """推送摘要：取最新一条；空目录 / 全部超龄 → None。"""
    import time as _time
    import netguard
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path))
    assert netguard.latest_sample_note() is None
    old = netguard.capture_sample("https://h.example.com/b", exc=OSError("down"))
    os.utime(old, (_time.time() - 25 * 3600,) * 2)
    assert netguard.latest_sample_note() is None          # 全部超龄不报
    netguard.capture_sample("https://h.example.com/a", resp=_FakeResp(429, b"x"))
    note = netguard.latest_sample_note()
    assert note and "status=429" in note and ".status/upstream/" in note


def test_main_us_failed_push_carries_sample_note(monkeypatch, tmp_path):
    """chain:us-failed 推送附最新失败现场采样与应急取数手册指引（2026-10-27）。"""
    import netguard
    import us_close_task as uct
    monkeypatch.setattr(netguard, "_sample_dir", lambda: str(tmp_path))
    netguard.capture_sample("https://web.ifzq.gtimg.cn/x", resp=_FakeResp(502, b"gw"))
    texts = []
    monkeypatch.setattr(uct, "sync_research_db", lambda: True)
    monkeypatch.setattr(uct, "wait_us_close", lambda: None)
    monkeypatch.setattr(uct, "run_us_fetch", lambda: False)
    monkeypatch.setattr(uct, "_notify", lambda t, text, **k: texts.append(text))
    monkeypatch.setattr(uct, "_log", lambda *a, **k: None)

    rc = uct.main()

    assert rc == 1
    assert any("上游失败现场已采样" in t for t in texts)
    assert any("docs/运维手册-上游应急取数.md" in t for t in texts)


# ------------------------------------------------- 看门狗日志清理（O-4 · 2026-09-27）

def test_watchdog_prune_logs_removes_only_expired(tmp_path, monkeypatch):
    """prune_logs：只删 mtime 超期文件；新日志与子目录（archive/）不动。"""
    import time as _time
    import watchdog
    logs = tmp_path / "logs"
    logs.mkdir()
    old, new = logs / "backfill.log", logs / "fetch-rotation.log"
    sub = logs / "archive"
    old.write_text("x")
    new.write_text("x")
    sub.mkdir()
    stale = _time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    monkeypatch.setattr(watchdog, "LOGS_DIR", str(logs))
    removed = watchdog.prune_logs(keep_days=30)
    assert removed == 1
    assert not old.exists() and new.exists() and sub.exists()


# ------------------------------------------------- 运营常量薄层（S-5 · 2026-09-27）

def test_app_config_defaults_bad_file_and_override(tmp_path, monkeypatch):
    """app_config：缺文件/坏 JSON 全回落默认；未知键滤掉；合法键覆盖；未知键取值抛。"""
    import app_config
    cases = [
        (str(tmp_path / "missing.json"), None),          # 缺文件
        (None, "{not json"),                              # 坏 JSON
    ]
    for fname, content in cases:
        p = tmp_path / ("cfg.json" if content is not None else "none.json")
        if content is not None:
            p.write_text(content, encoding="utf-8")
        monkeypatch.setattr(app_config, "CONFIG_PATH", str(p))
        monkeypatch.setattr(app_config, "_cache", None)
        assert app_config.get("recap_keep_days") == 400
        assert app_config.get("logs_keep_days") == 30
    ok = tmp_path / "ok.json"
    ok.write_text('{"recap_keep_days": 200, "unknown_key": 1}', encoding="utf-8")
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(ok))
    monkeypatch.setattr(app_config, "_cache", None)
    assert app_config.get("recap_keep_days") == 200   # 合法覆盖
    assert app_config.get("quant_jobs_keep") == 30    # 未覆盖键仍默认
    with pytest.raises(KeyError):                     # 未知键必须炸
        app_config.get("no_such_key")


# ------------------------------------------------- hithink 断源演练（S-4 · 2026-09-27）

def test_hithink_down_drill_and_recovery(monkeypatch):
    """断源演练固化为常驻回归：auth status 失败（key 失效的等效形态，桩掉 ht CLI，
    不依赖真实 CLI/网络/推送渠道）→ probe 如实记录 → 健康告警触发且带处置指引；
    恢复后告警消失。"""
    import types
    import server_context   # noqa: F401  需要真实 _hithink_probe/_health_alerts 口径

    def _boom(*_a, **_k):
        raise RuntimeError("ht auth status 失败: 401: token expired (drill)")
    monkeypatch.setitem(sys.modules, "ht", types.SimpleNamespace(ht=_boom))
    rec = server_context._hithink_probe()
    assert rec["present"] is True and rec["ok"] is False
    assert "401" in (rec.get("error") or "")
    alerts = server_context._health_alerts({"hithink": dict(rec)})
    assert "hithink.down" in [k for k, _ in alerts]
    assert any("auth login" in m for _, m in alerts)   # 处置指引随告警下发

    # 恢复路径：探活成功 → 不再告警
    monkeypatch.setitem(sys.modules, "ht", types.SimpleNamespace(
        ht=lambda *a, **k: {"user": "drill", "plan": "pro"}))
    rec2 = server_context._hithink_probe()
    assert rec2["ok"] is True
    assert "hithink.down" not in [k for k, _ in
                                  server_context._health_alerts({"hithink": dict(rec2)})]


# ------------------------------------------------- hithink CLI 定位兜底（2026-09-28）

def test_hithink_cli_find_exe(monkeypatch, tmp_path):
    """PATH 缺 %APPDATA%\\npm 的事故形态（2026-09-28）：which 落空时兜底显式查
    npm 全局目录应能找到；PATH 正常时优先 which；目录真没有则如实返回 None。"""
    import hithink_cli

    # ① PATH 正常：直接命中，不走到兜底
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    f = fake_bin / "hithink-finance.cmd"
    f.write_text("@echo off\r\n", encoding="utf-8")
    f.chmod(0o755)                       # Linux CI 腿 which 需要 X_OK
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("APPDATA", str(tmp_path / "nope"))
    # Windows 的 which 按环境变量 PATHEXT 的书写大小写拼扩展名（.CMD），比路径用 normcase
    assert os.path.normcase(hithink_cli.find_exe()) == os.path.normcase(str(f))

    # ② PATH 落空、APPDATA\npm 下有 → 兜底命中
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "hithink-finance.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert hithink_cli.find_exe() == str(npm / "hithink-finance.cmd")

    # ③ 两处都没有 → None（CLI 确实未安装）
    monkeypatch.setenv("APPDATA", str(tmp_path / "nope"))
    assert hithink_cli.find_exe() is None


# ---------------- server_context：端口共享绑定检测（2026-09-30 打不开事故防回退） ----------------
# 事故形状：旧实例绑 0.0.0.0:8000，新实例 SO_REUSEADDR 静默再绑 127.0.0.1:8000，
# 回环流量（浏览器/watchdog）被新实例劫持成空响应。防线：启动时 netstat 自查，
# 见「同端口外来监听者」即拒绝启动——解析器与活体双绑定各一层保护。

from server_context import _parse_netstat_listeners, _foreign_listeners  # noqa: E402

_NETSTAT_SAMPLE = "\r\n".join([
    "  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       19184",
    "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       3008",
    "  TCP    127.0.0.1:8000         127.0.0.1:7212         TIME_WAIT       0",
    "  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       555",
    "  TCP    [::]:8000              [::]:0                 LISTENING       19184",
    "  UDP    127.0.0.1:8000         *:*                                    666",
    "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       3008",
])  # 末行 = 自身 PID（v4），应被排除


def test_parse_netstat_foreign_listeners_finds_hijacker():
    # 自身 3008（新实例）视角：v4 通配 + v6 通配都是旧实例 19184 → 去重后正好点名它
    assert _parse_netstat_listeners(_NETSTAT_SAMPLE, 8000, 3008) == [19184]


def test_parse_netstat_foreign_listeners_symmetry_and_noise():
    # 反向视角（旧实例自查时新实例已绑上）：同样能看见对方
    assert _parse_netstat_listeners(_NETSTAT_SAMPLE, 8000, 19184) == [3008]
    # 无外来者（端口上只有自己）→ 空表，正常启动放行
    assert _parse_netstat_listeners(_NETSTAT_SAMPLE.replace("19184", "3008"), 8000, 3008) == []
    # 端口无监听 → 空
    assert _parse_netstat_listeners(_NETSTAT_SAMPLE, 9999, 3008) == []
    # :18000 不得因 endswith 误配 :8000
    assert _parse_netstat_listeners(_NETSTAT_SAMPLE, 18000, 999) == [555]
    # TIME_WAIT / UDP / 残缺行不算监听占用
    assert _parse_netstat_listeners(
        "TCP 1.2.3.4:8000 5.6.7.8:9 TIME_WAIT 42\r\n"
        "UDP 1.2.3.4:8000 *:* 42\r\ngarbage\r\n\r\n", 8000, 3008) == []


@pytest.mark.skipif(os.name != "nt", reason="netstat -ano 为 Windows 口径")
def test_foreign_listeners_live_double_bind():
    # 活体复刻事故：本进程持端口在先，子进程以 SO_REUSEADDR 再绑同端口（与
    # 2026-09-30 的僵尸实例完全同款）→ 自查必须把子进程 PID 点名报出来。
    import socket
    import subprocess
    import sys
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 先绑者也带 SO_REUSEADDR：与 http.server（allow_reuse_address=1）同款——
    # Windows 上后者能压上来重复绑定的前提正是先绑者开了它（实测不带则 10013）。
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,socket,time;"
         f"s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
         f"s.bind(('127.0.0.1',{port}));s.listen(1);"
         "print('ok',os.getpid(),flush=True);time.sleep(15)"],
        stdout=subprocess.PIPE)
    try:
        line = child.stdout.readline().strip()   # 子进程已绑定完成
        assert line.startswith(b"ok")
        # netstat 点名的是真正持有套接字的解释器 PID：venv 的 python.exe 可能是
        # 启动器壳（uv 系），Popen 的 child.pid 是壳、真身是它再 spawn 的子进程——
        # 故由子进程自报 os.getpid()，不拿 child.pid 硬对（2026-10-06 修复）。
        child_pid = int(line.split()[1])
        assert _foreign_listeners(port) == [child_pid]
    finally:
        child.kill()
        child.wait()
        holder.close()


# ---------------- 研究引擎档位闸门（C15 候选 A：情绪过热降级，2026-10-16） ---------

def _regime_mods():
    """导入 strategy-iter 的 indicators（研究库不在 conftest 路径里，这里显式加）。"""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (os.path.join(root, "strategy-iter"), os.path.join(root, "strategy-iter", "engine")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import indicators  # noqa: PLC0415
    return indicators


def _rrow(lu, adv, ld, prev3=None, **kw):
    d = {"lu_cnt": lu, "adv_ratio": adv, "ld_cnt": ld, "amt_ratio": 1.0}
    if prev3 is not None:
        d["lu_prev3"] = prev3
    d.update(kw)
    return d


def test_regime_base_unchanged_without_gate():
    """不配置 overheat_downgrade 时判定与 C6/C7 原口径逐字节一致（回归护栏）。"""
    ind = _regime_mods()
    cfg = {"panic_ld": 30, "aggressive_lu": 75, "aggressive_adv": 0.52,
           "aggressive_ld_max": 15, "freeze_lu": 25, "freeze_adv": 0.3,
           "defensive_lu": 45, "defensive_adv": 0.4}
    assert ind.classify_regime(_rrow(89, 0.83, 2, 90.0), cfg) == "aggressive"
    assert ind.classify_regime(_rrow(51, 0.34, 19, 83.7), cfg) == "defensive"
    assert ind.classify_regime(_rrow(20, 0.2, 5, 40.0), cfg) == "freeze"
    assert ind.classify_regime(_rrow(31, 0.3, 5, 40.0), cfg) == "defensive"
    assert ind.classify_regime(_rrow(55, 0.5, 5, 40.0), cfg) == "normal"


def test_regime_overheat_downgrade_gate():
    """候选 A 闸门：涨停家数 > 前3日均值×ratio 时档位降一档；缺 lu_prev3 时不降。"""
    ind = _regime_mods()
    cfg = {"panic_ld": 30, "aggressive_lu": 75, "aggressive_adv": 0.52,
           "aggressive_ld_max": 15, "freeze_lu": 25, "freeze_adv": 0.3,
           "defensive_lu": 45, "defensive_adv": 0.4,
           "overheat_downgrade": {"ratio": 1.1}}
    # 89 涨停 vs 前3日均 40.7（×2.2）→ aggressive 降为 normal
    assert ind.classify_regime(_rrow(89, 0.83, 2, 40.7), cfg) == "normal"
    # 未过热（89 vs 90）→ 保持 aggressive
    assert ind.classify_regime(_rrow(89, 0.83, 2, 90.0), cfg) == "aggressive"
    # 恰在阈值上（89 vs 80.9=89/1.1）→ 不降（严格大于）
    assert ind.classify_regime(_rrow(89, 0.83, 2, 89 / 1.1), cfg) == "aggressive"
    # 降级链：defensive→freeze；freeze 保持 freeze
    assert ind.classify_regime(_rrow(51, 0.34, 19, 20.0), cfg) == "freeze"
    assert ind.classify_regime(_rrow(20, 0.2, 5, 5.0), cfg) == "freeze"
    # 前 3 日均值缺失（区间头部/数据缺口）→ 不降级，宁可漏判不可误判
    assert ind.classify_regime(_rrow(89, 0.83, 2), cfg) == "aggressive"
    # 恐慌闸门优先级更高：先 forced defensive，再降一档 → freeze
    assert ind.classify_regime(_rrow(89, 0.83, 35, 40.7), cfg) == "freeze"


def test_add_regime_computes_lu_prev3_without_today():
    """lu_prev3 = 前 3 个交易日均值（shift(1)），不含当日（无未来函数）。"""
    ind = _regime_mods()
    import pandas as pd
    ms = pd.DataFrame({"lu_cnt": [10.0, 20.0, 30.0, 40.0, 100.0],
                       "adv_ratio": [0.5] * 5, "ld_cnt": [5] * 5,
                       "amt_ratio": [1.0] * 5, "max_height": [3] * 5,
                       "height_chg": [0] * 5})
    out = ind.add_regime(ms, {"panic_ld": 30, "aggressive_lu": 75, "aggressive_adv": 0.52,
                              "aggressive_ld_max": 15, "freeze_lu": 25, "freeze_adv": 0.3,
                              "defensive_lu": 45, "defensive_adv": 0.4})
    p3 = out["lu_prev3"].tolist()
    assert pd.isna(p3[0]) and pd.isna(p3[1])
    assert p3[3] == 20.0          # (10+20+30)/3
    assert p3[4] == 30.0          # (20+30+40)/3 —— 当日 100 不参与


def test_is_overheat_gate_shared_predicate():
    """过热判定（候选 A 系共用）：比例可配、缺值不判、前3日均值≤0 不判。"""
    ind = _regime_mods()
    cfg = {"overheat_downgrade": {"ratio": 1.1}}
    assert ind.is_overheat(_rrow(89, 0.8, 2, 40.7), cfg) is True
    assert ind.is_overheat(_rrow(89, 0.8, 2, 90.0), cfg) is False
    assert ind.is_overheat(_rrow(89, 0.8, 2), cfg) is False          # 无 lu_prev3
    assert ind.is_overheat(_rrow(89, 0.8, 2, 0.0), cfg) is False     # 均值为 0（除零保护）
    assert ind.is_overheat(_rrow(89, 0.8, 2, 40.7), {}) is False     # 未配置闸门
    assert ind.is_overheat(_rrow(89, 0.8, 2, 85.0),
                           {"overheat_lu_delta": {"ratio": 1.0}}) is True   # 另一键名


def test_run_round_rate_handles_missing_rows():
    """统计口径修复（2026-10-16）：含「停牌/数据缺失」行时布尔率不得失真。

    缺失行的 dict 缺布尔键 → 构造 DataFrame 后该列变 object dtype，未修复时
    .mean()*100 把 31% 显示成 0.21%（本轮扩窗首次暴露）。
    """
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.join(root, "strategy-iter") not in sys.path:
        sys.path.insert(0, os.path.join(root, "strategy-iter"))
    import pandas as pd
    from engine.run_round import _rate, _grp_stats
    v = pd.DataFrame([
        {"win": True, "buy_triggered": True, "risk_hit": False, "strong_sector": True,
         "strong_concept": True, "logic_realized": True, "close_ret": 3.0, "high_ret": 5.0,
         "low_ret": -1.0, "open_ret": 1.0, "result_score": 80.0, "logic_score": 70.0,
         "composite": 75.0, "rating": "A"},
        {"win": False, "buy_triggered": False, "risk_hit": True, "strong_sector": False,
         "strong_concept": False, "logic_realized": False, "close_ret": -2.0, "high_ret": 1.0,
         "low_ret": -6.0, "open_ret": 0.5, "result_score": 40.0, "logic_score": 50.0,
         "composite": 45.0, "rating": "D"},
        {"win": None, "close_ret": float("nan")},        # 停牌/数据缺失行（缺布尔键）
    ])
    assert v["buy_triggered"].dtype == object                 # 复现失真前提
    assert _rate(v, "buy_triggered") == 50.0                  # 旧实现给出 0.21 量级的错值
    assert _rate(v, "risk_hit") == 50.0
    assert _rate(v, "strong_sector") == 50.0
    assert _rate(v, "strong_concept") == 50.0
    assert _rate(v, "not_a_column") is None                   # 列不存在 → None（非 0）
    assert _rate(pd.DataFrame({"x": [None, float("nan")]}), "x") is None   # 全 NaN → None
    s = _grp_stats(v)
    assert s["n"] == 2 and s["buy_trigger_rate"] == 50.0 and s["win_rate"] == 50.0


if __name__ == "__main__":
    # 直跑入口：委托给 pytest（conftest 的路径引导已在上方先行生效）
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------- 制度口径（A 股 T+1）可执行收益：spec_exec_t2（2026-10-16） ---------

def test_exec_t2_entry_uses_frozen_window():
    """买点判定沿用 execution_layer.MF_BUY 冻结窗口（单变量：不另立一套窗口）。"""
    import spec_exec_t2 as x
    assert x.entry_of("lu", 1.0, 3.0)[0] == 1.0            # 窗内 → 开盘成交
    assert x.entry_of("lu", -0.5, 3.0)[0] is None          # 低开不接
    assert x.entry_of("lu", 5.5, 9.0)[0] is None           # 高开>+5% 放弃
    assert x.entry_of("lu", None, 3.0)[0] is None
    assert x.entry_of("nlu", 2.0, 2.5)[0] == 2.0           # 窗内高开 → 开盘成交
    assert x.entry_of("nlu", -1.0, 0.5)[0] == 0.0          # 低开但盘中回到昨收 → 成交
    assert x.entry_of("nlu", -1.0, -0.5)[0] is None        # 限价未触及
    assert x.entry_of("nlu", -3.0, 1.0)[0] is None         # 超出 -2% 窗


def test_exec_t2_chain_and_net():
    """T+2 由两段涨幅链式相乘得到（ohlc_rets(T2) 是相对 T+1 收盘）。"""
    import spec_exec_t2 as x
    assert abs(x.chain(10.0, 10.0) - 21.0) < 1e-9           # 1.1*1.1-1（浮点近似）
    assert abs(x.chain(0.0, -5.0) + 5.0) < 1e-9
    assert x.chain(None, 3.0) is None
    r1 = {"o": 2.0, "h": 4.0, "l": -1.0, "c": 3.0}
    net, why = x.t2_net("lu", r1, {"o": 1.0, "c": 2.0})
    # 建仓 +2% → T+2 开盘相对昨收 = (1.03*1.01-1)=4.03% → 相对建仓 (1.0403/1.02-1)=1.990%
    assert abs(net - (1.9903 - 0.15)) < 0.01 and why == "T+2 开盘离场"
    assert x.t2_net("lu", {"o": 6.0, "h": 8.0, "l": 5.0, "c": 7.0}, {"o": 1.0, "c": 1.0})[0] is None
    assert x.t2_net("lu", r1, None)[0] is None


def test_exec_t2_risk_low_is_exposure_not_exit():
    """T+1 破 risk_low 只记风险暴露——A 股当日不可卖，故不改变离场价（与诊断口径的差别）。"""
    import spec_exec_t2 as x
    r1 = {"o": 2.0, "h": 4.0, "l": -6.0, "c": -3.0}        # T+1 盘中破 -5%
    net, why = x.t2_net("lu", r1, {"o": 0.0, "c": 0.0})
    assert why == "T+2 开盘离场"                             # 不按 -5% 止损离场
    assert net is not None


def test_exec_t2_limit_down_open_falls_back_to_close():
    """T+2 跌停开盘卖不出 → 改按收盘价（并标注），不是假装能开盘成交。"""
    import spec_exec_t2 as x
    r1 = {"o": 1.0, "h": 2.0, "l": 0.0, "c": 1.0}
    r2 = {"o": -9.9, "c": -5.0}                             # 相对 T+1 收盘跌停开盘
    net, why = x.t2_net("lu", r1, r2)
    assert "改收盘离场" in why
    exp = x._ratio(x.chain(1.0, -5.0), 1.0) - x.FEE_PCT
    assert abs(net - round(exp, 3)) < 0.01


def test_exec_t2_stats_and_rolling_summary():
    import spec_exec_t2 as x
    rows = [
        {"group": "lu", "exec_t2": {"net": 2.0, "risk_low_hit": False}},
        {"group": "lu", "exec_t2": {"net": -3.0, "risk_low_hit": True}},
        {"group": "nlu", "exec_t2": {"net": None, "note": "未成交"}},
    ]
    s = x.exec_t2_stats(rows)
    assert s["lu"]["n_exec"] == 2 and s["lu"]["pnl_avg"] == -0.5 and s["lu"]["win_rate"] == 50.0
    assert s["lu"]["risk_exposed"] == 1
    assert s["nlu"]["n"] == 1 and s["nlu"]["n_exec"] == 0
    assert s["overall"]["n"] == 3 and s["overall"]["n_exec"] == 2
    vals = {
        "20260910": {"rules": "c7", "picks": [{"group": "lu", "exec_t2": {"net": 1.0}}]},
        "20260911": {"rules": "cf", "picks": [{"group": "lu", "exec_t2": {"net": 99.0}}]},
        "20260912": {"rules": "c7", "picks": [{"group": "lu"}]},        # 未补算
    }
    r = x.rolling_summary(vals, days=20, rules="c7")
    assert r["overall"]["n_exec"] == 1 and r["overall"]["pnl_avg"] == 1.0
    assert r["days"] == 1 and r["date_to"] == "20260912"   # 窗口上界；days 只数已补算的日
    assert "制度口径" in r["label"] and r["fee"] == 0.15
    assert x.rolling_summary({"20260912": vals["20260912"]}, rules="c7") is None


def test_exec_diag_stats_carries_non_executable_label():
    """诊断口径带明确标注：不可执行（防把「均益」读成到手的钱）。"""
    import spec_validate as sv
    out = sv._val_exec_stats([{"group": "lu", "open": 1.0, "high": 3.0, "low": -6.0,
                               "close": -2.0}])
    assert "不可执行" in out["_label"] and "exec_t2" in out["_executable_see"]


def test_record_validation_wires_exec_t2_summary(monkeypatch, tmp_path):
    """端到端接线：写台账时补算制度口径并把滚动汇总回填到 payload（不进判定）。"""
    import json
    import spec_validate as sv
    import spec_exec_t2 as x
    track_path = tmp_path / "pool_track.json"
    vals = {
        # 09-10 验的是 09-09 池；09-11 验 09-10 池 → 09-10 记录的 T+2 就是 09-11
        "20260910": {"date": "20260909", "rules": "c7", "picks": [
            {"code": "600744", "group": "lu", "open": 2.0, "high": 4.0, "low": -1.0,
             "close": 3.0, "win": True}]},
        "20260911": {"date": "20260910", "rules": "c7", "picks": [
            {"code": "600644", "group": "lu", "open": -1.0, "high": 1.0, "low": -5.0,
             "close": -4.0, "win": False}]},
    }
    track_path.write_text(json.dumps({"validations": vals}), encoding="utf-8")
    monkeypatch.setattr(sv, "POOL_TRACK", str(track_path))
    monkeypatch.setattr(sv.snapio, "list_dates", lambda: ["20260911", "20260910"])
    monkeypatch.setattr(x, "t2_rets", lambda d8, codes: {"600744": {"o": 1.0, "c": 2.0}})
    payload = {"ok": True, "date": "20260911", "rules": "c7",
               "picks": [{"code": "600644", "group": "lu"}]}
    summary = sv.record_validation("20260911", payload)
    assert summary and summary["overall"]["n_exec"] == 1
    assert abs(summary["overall"]["pnl_avg"] - (1.99029 - 0.15)) < 0.01
    saved = json.loads(track_path.read_text(encoding="utf-8"))
    assert saved["validations"]["20260910"]["picks"][0]["exec_t2"]["net"] is not None
    # 错配记录不进诊断汇总（09-11 验 09-10 池 → 应验 09-10，匹配）
    assert sv._track_for_diag({"validations": vals}) == vals
    bad = dict(vals)
    bad["20260911"] = {"date": "20260909", "rules": "c7", "picks": []}
    assert "20260911" not in sv._track_for_diag({"validations": bad})


# ---------------- us_close_task：缺口兜底挑选（2026-10-21 漏跑补跑）----------------

from us_close_task import GAP_LOOKBACK, _pick_gaps  # noqa: E402


def test_pick_gaps_basic():
    # 窗口内 1 个「可终版化且池未终版」→ 只挑它（升序返回）
    cand = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    got = _pick_gaps(cand, lambda d: d == "2026-10-20", lambda d: d == "2026-10-20")
    assert got == ["2026-10-20"]


def test_pick_gaps_filters():
    cand = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    # 场次未收盘（eligible=False）不进缺口——补跑只会再产出一个 pending 占位
    got = _pick_gaps(cand, lambda d: d != "2026-10-20", lambda d: True)
    assert got == ["2026-10-15", "2026-10-16", "2026-10-19"]
    # 已终版（pending=False）不进缺口
    got = _pick_gaps(cand, lambda d: True, lambda d: d == "2026-10-16")
    assert got == ["2026-10-16"]
    # 条件都不满足 → 空
    assert _pick_gaps(cand, lambda d: False, lambda d: True) == []
    assert _pick_gaps(cand, lambda d: True, lambda d: False) == []


def test_pick_gaps_window_cap():
    # 7 个候选、回溯上限 6 → 只回溯最近 6 个（防机器久停后无界补跑）
    cand = [f"2026-10-{i:02d}" for i in range(10, 17)]
    assert len(cand) == 7 and GAP_LOOKBACK == 6
    got = _pick_gaps(cand, lambda d: True, lambda d: True)
    assert got == cand[-GAP_LOOKBACK:]
    assert cand[0] not in got


def test_pick_gaps_limit_and_purity():
    cand = ["a", "b", "c", "d"]
    assert _pick_gaps(cand, lambda d: True, lambda d: True, limit=2) == ["c", "d"]
    assert cand == ["a", "b", "c", "d"]   # 不改入参
    assert _pick_gaps([], lambda d: True, lambda d: True) == []


def test_pool_not_final_accepts_iso(tmp_path, monkeypatch):
    """回归：main 传 ISO 日期（2026-10-20），pool_not_final 须自行转 8 位快照键——
    否则读不到快照一律 False，缺口会被漏检（2026-10-21 演练实证）。"""
    import json
    import snapio
    import us_close_task as uct
    monkeypatch.setattr(snapio, "RECAP_DATA", str(tmp_path))

    def write(date8, pool):
        doc = {"modules": {"speculation": {"data": {"pool": pool}}}}
        (tmp_path / (date8 + ".json")).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    write("20261020", {"stage": "pending", "picks": [],
                       "note": "隔夜美股场次未定（当前 us_date=2026-10-19，待 T 日场次收盘）"})
    write("20261019", {"stage": None, "picks": [{"代码": "600001"}],
                       "note": "C7 主板概念口径（strategy-iter C1→C3…）"})
    write("20261016", {"ok": False, "error": "RuntimeError: boom", "picks": []})

    assert uct.pool_not_final("2026-10-20") is True     # pending 占位
    assert uct.pool_not_final("2026-10-19") is False    # 真池（带口径 note）
    assert uct.pool_not_final("2026-10-16") is True     # error 池无 note = 未终版
    assert uct.pool_not_final("2026-10-15") is False    # 无快照 → 不补
    # 与 _pick_gaps 组合：ISO 口径贯通（场次已收盘 + 未终版 → 进缺口）
    got = _pick_gaps(["2026-10-19", "2026-10-20"], lambda d: True, uct.pool_not_final)
    assert got == ["2026-10-20"]


def test_main_skips_finalization_when_us_fetch_fails(monkeypatch):
    """加严（2026-10-21）：us_market 抓取（含重试）失败必须中止本次终版结算——否则
    factors 陈旧时 row_freshness 仍会因「场次已按墙钟收盘」放行，池会被按过期隔夜
    美股数据标成终版（C6 闸门取值偏）。缺口留给下一次链自动补。"""
    import us_close_task as uct

    runs, notes = [], []
    monkeypatch.setattr(uct, "sync_research_db", lambda: True)
    monkeypatch.setattr(uct, "wait_us_close", lambda: None)
    monkeypatch.setattr(uct, "run_us_fetch", lambda: False)      # 抓取失败
    monkeypatch.setattr(uct, "run_step", lambda cmd: (runs.append(cmd[-1]) or True))
    monkeypatch.setattr(uct, "_notify", lambda *a, **k: notes.append(k.get("key")))
    monkeypatch.setattr(uct, "_log", lambda *a, **k: None)

    rc = uct.main()

    assert rc == 1
    assert runs == []                    # 抓取失败 → 一个后续步骤都没跑
    assert notes == ["chain:us-failed"]


def test_run_us_fetch_retries_once_then_succeeds(monkeypatch):
    """开机触发时网络可能未就绪：us_market 失败须重试一次（等待时长可注入为空）。"""
    import us_close_task as uct

    seq, calls, waits = [False, True], [], []
    monkeypatch.setattr(uct, "run_step", lambda cmd: (calls.append(list(cmd)) or seq.pop(0)))
    monkeypatch.setattr(uct.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(uct, "_log", lambda *a, **k: None)

    assert uct.run_us_fetch() is True
    assert len(calls) == 2 and calls[0] == calls[1]
    assert waits == [uct.US_FETCH_RETRY_S]


def test_run_us_fetch_gives_up_after_one_retry(monkeypatch):
    import us_close_task as uct

    seq, calls = [False, False], []
    monkeypatch.setattr(uct, "run_step", lambda cmd: (calls.append(list(cmd)) or seq.pop(0)))
    monkeypatch.setattr(uct.time, "sleep", lambda s: None)
    monkeypatch.setattr(uct, "_log", lambda *a, **k: None)
    assert uct.run_us_fetch() is False
    assert len(calls) == 2               # 只重试一次，不无限重试

    seq2, calls2 = [True], []            # 首跑成功 → 不重试
    monkeypatch.setattr(uct, "run_step", lambda cmd: (calls2.append(list(cmd)) or seq2.pop(0)))
    assert uct.run_us_fetch() is True and len(calls2) == 1
