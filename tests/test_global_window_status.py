# -*- coding: utf-8 -*-
"""global 热力图窗口状态单测（复盘 §7/§9 N1/§10「ok 由 expected/actual 计算」）。

覆盖：
- window_status.classify/summary/log_line 矩阵（0/N、部分、全量、脏数据不抛错）；
- _add_windows_us / _add_windows_cn 直接调用（stub 取数）：0 只 / 全失败 /
  部分成功 / 全成功四种输入，断言 status 与 n_ok/n_total；
- fetch_heat_us / fetch_heat_cn 的 payload 都带 "windows"；
- _run 写盘：windows 只在 heatmap 块内，errors 不含 windows；partial/unknown
  走 LOG.warning、ok 走 LOG.info；
- 源码层面：不再有手写 `LOG.info("[ok] heatmap_*")`，且 _keep 降级字面量保留
  （tests/smoke.py:792 依赖）。

纪律：零真实网络/零 hithink 调用；fetch_global 导入会 rebind sys.stdout/stderr，
module fixture 负责保存与恢复（wrapper 保活，避免 GC close 掉 pytest 捕获 buffer）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "global"))

import window_status  # noqa: E402

_KEEPALIVE = []   # 保活 fetch_global 导入时新建的 TextIOWrapper（见 fixture 注释）

CLOSES6 = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5]   # 6 条才有 pct5（_chg 要求 len>5）


@pytest.fixture(scope="module")
def fg():
    """隔离 import 副作用：保存 sys.stdout/stderr，导入后恢复。

    TextIOWrapper 被解引用时 __del__ 会 close 底层 buffer；直接丢弃会关掉
    pytest 捕获流，故把新 wrapper 放进模块级 _KEEPALIVE 保活。
    """
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        import fetch_global as mod
    finally:
        new_out, new_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = saved_out, saved_err
        for w in (new_out, new_err):
            if w is not saved_out and w is not saved_err:
                _KEEPALIVE.append(w)
    return mod


def _raise(*_a, **_k):
    raise RuntimeError("boom")


class _FakeLog:
    """收集 info/warning 的假 logger（_run 的日志级别断言用）。"""

    def __init__(self):
        self.info_lines = []
        self.warning_lines = []

    def info(self, msg, *a, **k):
        self.info_lines.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warning_lines.append(str(msg))


# ---------------------------------------------------------------- classify 矩阵

@pytest.mark.parametrize("n_ok,n_total", [(0, 0), (0, 717), (-3, 10), (None, None)])
def test_classify_unknown(n_ok, n_total):
    """0 只 / 0 行 / 负数 / None：一律 unknown，绝不伪装 ok。"""
    assert window_status.classify(n_ok, n_total) == window_status.UNKNOWN


def test_classify_inf_is_unknown():
    """float('inf') 是脏数据：按 0 夹紧，不抛 OverflowError。"""
    assert window_status.classify(float("inf"), 10) == window_status.UNKNOWN
    assert window_status.summary("x", float("inf"), 10)["status"] == window_status.UNKNOWN


def test_classify_partial():
    assert window_status.classify(1, 717) == window_status.PARTIAL


@pytest.mark.parametrize("n_ok,n_total", [(717, 717), (5, 3)])
def test_classify_ok(n_ok, n_total):
    """全覆盖（或脏数据多报）→ ok；ok 只能由实际覆盖算出。"""
    assert window_status.classify(n_ok, n_total) == window_status.OK


def test_summary_and_log_line():
    s = window_status.summary("cn", 0, 717)
    assert s == {"label": "cn", "status": "unknown", "n_ok": 0, "n_total": 717}
    assert window_status.log_line("heatmap_cn", s) == \
        "heatmap_cn windows=0/717 status=unknown"
    # 脏输入不抛错，按 0 夹紧
    assert window_status.summary("cn", -1, None)["n_ok"] == 0
    assert window_status.log_line("heatmap_cn", None) == \
        "heatmap_cn windows=0/0 status=unknown"


# ---------------------------------------------------------------- _add_windows_us

def _us_sectors(codes):
    return [{"stocks": [{"code": c} for c in codes]}]


def test_add_windows_us_zero(fg):
    w = fg._add_windows_us([])
    assert (w["status"], w["n_ok"], w["n_total"]) == ("unknown", 0, 0)


def test_add_windows_us_all_fail(fg, monkeypatch):
    monkeypatch.setattr(fg, "_us_daily_closes", _raise)
    w = fg._add_windows_us(_us_sectors(["S0", "S1"]))
    assert (w["status"], w["n_ok"], w["n_total"]) == ("unknown", 0, 2)


def test_add_windows_us_partial(fg, monkeypatch):
    def stub(sym):
        if sym == "s0":
            return list(CLOSES6)
        raise RuntimeError("boom")

    monkeypatch.setattr(fg, "_us_daily_closes", stub)
    w = fg._add_windows_us(_us_sectors(["S0", "S1"]))
    assert (w["status"], w["n_ok"], w["n_total"]) == ("partial", 1, 2)


def test_add_windows_us_all_ok(fg, monkeypatch):
    monkeypatch.setattr(fg, "_us_daily_closes", lambda sym: list(CLOSES6))
    w = fg._add_windows_us(_us_sectors(["S0", "S1"]))
    assert (w["status"], w["n_ok"], w["n_total"]) == ("ok", 2, 2)


# ---------------------------------------------------------------- _add_windows_cn

def _cn_sectors(codes):
    return [{"name": "行业", "stocks": [{"code": c} for c in codes]}]


def _cn_rows():
    return {"item": [{"date_ms": i, "close_price": 10 + i} for i in range(6)]}


def test_add_windows_cn_zero(fg, monkeypatch):
    monkeypatch.setattr(fg.time, "sleep", lambda *_: None)
    w = fg._add_windows_cn([], {})
    assert (w["status"], w["n_ok"], w["n_total"]) == ("unknown", 0, 0)


def test_add_windows_cn_all_fail(fg, monkeypatch):
    monkeypatch.setattr(fg.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fg.ht, "ht", _raise)
    w = fg._add_windows_cn(_cn_sectors(["600000", "600001"]), {})
    assert (w["status"], w["n_ok"], w["n_total"]) == ("unknown", 0, 2)


def test_add_windows_cn_partial(fg, monkeypatch):
    monkeypatch.setattr(fg.time, "sleep", lambda *_: None)

    def stub(*a, **k):
        if "600001.SH" in a:   # thscode(600001) == 600001.SH
            return _cn_rows()
        raise RuntimeError("boom")

    monkeypatch.setattr(fg.ht, "ht", stub)
    w = fg._add_windows_cn(_cn_sectors(["600000", "600001"]), {})
    assert (w["status"], w["n_ok"], w["n_total"]) == ("partial", 1, 2)


def test_add_windows_cn_all_ok(fg, monkeypatch):
    monkeypatch.setattr(fg.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fg.ht, "ht", lambda *a, **k: _cn_rows())
    w = fg._add_windows_cn(_cn_sectors(["600000", "600001"]), {})
    assert (w["status"], w["n_ok"], w["n_total"]) == ("ok", 2, 2)


# ---------------------------------------------------------------- fetch_heat_* payload

def test_fetch_heat_cn_wiring(fg, monkeypatch):
    monkeypatch.setattr(fg, "_latest_industry_map", lambda: ({}, ""))
    monkeypatch.setattr(fg.ht, "market_snapshot_all", lambda *a, **k: [
        {"ticker": "600000", "last_price": 1, "turnover": 1e8,
         "price_change_ratio_pct": 1.0}])
    monkeypatch.setattr(fg.ht, "symbol_names", lambda *a, **k: {})
    monkeypatch.setattr(fg, "_cn_snapshot_date8", lambda *a, **k: "")
    monkeypatch.setattr(fg, "_add_windows_cn",
                        lambda sectors, board_daily=None:
                        window_status.summary("cn", 0, 1))

    payload = fg.fetch_heat_cn({})
    assert payload["windows"]["status"] == "unknown"
    assert payload["windows"]["n_ok"] == 0 and payload["windows"]["n_total"] == 1
    assert payload["sectors"]
    assert payload["asof"] == "" and payload["map_vintage"] == ""


class _R:
    encoding = "utf-8"
    text = ('var hq_str_gb_aapl="Apple,100.00,1.50,2026-11-21 16:00:00,'
            '0,0,0,0,0,0,0,0,1000000";')


def test_fetch_heat_us_wiring(fg, monkeypatch):
    monkeypatch.setattr(fg, "_retry", lambda fn, *a, **k: _R())
    monkeypatch.setattr(fg, "_add_windows_us",
                        lambda sectors: window_status.summary("us", 1, 1))

    payload = fg.fetch_heat_us()
    assert payload["windows"]["status"] == "ok"
    assert payload["sectors"]
    assert payload["asof"] == "2026-11-21 16:00:00"


# ---------------------------------------------------------------- _log_windows 级别

def test_log_windows_levels(fg, monkeypatch):
    fake = _FakeLog()
    monkeypatch.setattr(fg, "LOG", fake)
    fg._log_windows("heatmap_cn", window_status.summary("cn", 5, 5))
    fg._log_windows("heatmap_cn", window_status.summary("cn", 0, 5))
    fg._log_windows("heatmap_cn", window_status.summary("cn", 1, 5))
    assert fake.info_lines == ["[ok] heatmap_cn windows=5/5 status=ok"]
    assert fake.warning_lines == [
        "[warn] heatmap_cn windows=0/5 status=unknown",
        "[warn] heatmap_cn windows=1/5 status=partial",
    ]


# ---------------------------------------------------------------- _run 集成

def test_run_wiring(fg, monkeypatch, tmp_path):
    out = tmp_path / "global.json"
    fake = _FakeLog()
    monkeypatch.setattr(fg, "OUT", str(out))
    monkeypatch.setattr(fg, "_load_board_daily", lambda n: {})
    monkeypatch.setattr(fg, "fetch_indices", lambda: ({"x": {}}, {}))
    monkeypatch.setattr(fg, "fetch_radar", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(fg, "fetch_heat_us", lambda: {
        "sectors": [], "windows": window_status.summary("us", 0, 0)})
    monkeypatch.setattr(fg, "fetch_heat_cn", lambda *a, **k: {
        "sectors": [], "windows": window_status.summary("cn", 0, 717)})
    monkeypatch.setattr(fg, "fetch_intraday", lambda: {})
    monkeypatch.setattr(fg, "LOG", fake)

    fg._run()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["heatmap_us"]["windows"]["status"] == "unknown"
    assert data["heatmap_cn"]["windows"]["status"] == "unknown"
    assert data["errors"] == {}
    assert not any("windows" in k for k in data["errors"])
    # 顶层不加 windows（只在各 heatmap 块内）
    assert "windows" not in data
    # 0/N 不得再以 [ok] 出现；两个 unknown 各一条 warning
    assert len(fake.warning_lines) >= 2
    assert any("heatmap_us" in m and "status=unknown" in m for m in fake.warning_lines)
    assert any("heatmap_cn" in m and "status=unknown" in m for m in fake.warning_lines)
    assert not any(m.startswith("[ok] heatmap_") for m in
                   fake.info_lines + fake.warning_lines)


# ---------------------------------------------------------------- 源码断言

def test_source_no_hardcoded_ok():
    src = (ROOT / "backend" / "global" / "fetch_global.py").read_text(encoding="utf-8")
    assert 'LOG.info("[ok] heatmap_cn")' not in src
    assert 'LOG.info("[ok] heatmap_us")' not in src
    assert '_keep("heatmap_cn")' in src        # tests/smoke.py:792 依赖
    assert '_keep("heatmap_us")' in src


# ---------------------------------------------------------------- 单文件 import 自足

def test_module_loadable_by_file_path_in_clean_interpreter():
    """importlib 单文件加载必须自足，不能靠别的用例先把 backend/global 塞进 sys.path。

    tests/test_units.py 的 _load_fetch_global 走 spec_from_file_location；本文件在
    import 期插了 sys.path，全量收集时会掩盖 fetch_global 自身找不到同目录
    window_status 的问题（曾导致 `pytest tests/test_units.py` 3 failed）。
    故用 -I 子进程复现干净 sys.path 再加载一次；加载器包装 stdout/stderr，
    避免 fetch_global 模块级重绑定碰坏子进程捕获通道。
    """
    p = ROOT / "backend" / "global" / "fetch_global.py"
    code = (
        "import io, sys\n"
        "sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')\n"
        "sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')\n"
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('fg_probe', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert hasattr(mod, 'window_status')\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    r = subprocess.run([sys.executable, "-I", "-c", code, str(p)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True,
                       errors="replace")
    assert r.returncode == 0, r.stderr
