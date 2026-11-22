# -*- coding: utf-8 -*-
"""告警可见性与送达去重（2026-11-22）：未配置/失败要出声，只有真送达才记当日。

覆盖 backend/notify.py 的 code 语义、degraded 健康摘要、同 code 1 小时日志/状态
去重，以及 backend/watchdog.py 的 _recap_alert「sent=False 不消耗当日额度」。
全部离线：配置/状态/日志路径都指到 tmp_path，渠道发送函数一律 stub，配置里
只出现明显占位串 "placeholder"，不含任何可用凭据。
"""
import json
import sys
import time
import types

import pytest

import notify
import watchdog


@pytest.fixture
def notify_env(tmp_path, monkeypatch):
    """把 notify 的配置/状态/日志全部隔离到 tmp_path，绝不碰真实 .status。"""
    monkeypatch.setattr(notify, "CONFIG_PATH", str(tmp_path / "notify.json"))
    monkeypatch.setattr(notify, "STATE_PATH", str(tmp_path / "notify_state.json"))
    monkeypatch.setattr(notify, "LOG_PATH", str(tmp_path / "logs" / "notify.log"))
    return tmp_path


def _write_config(tmp_path, cfg):
    (tmp_path / "notify.json").write_text(json.dumps(cfg), encoding="utf-8")


def _capture_logs(monkeypatch):
    logs = []

    def _fake_log(msg, level="info"):
        logs.append((msg, level))

    monkeypatch.setattr(notify, "_log", _fake_log)
    return logs


# ---------------------------------------------------------------- notify.send

def test_unconfigured_logs_and_no_dedup(notify_env, monkeypatch):
    """未配置渠道：ERROR 出声；同 code 1 小时内第二次调用不再写日志/状态。"""
    logs = _capture_logs(monkeypatch)

    res = notify.send("复盘快照缺失", "正文", key="k0")
    assert res["sent"] is False
    assert res["code"] == "not-configured"
    assert "未配置推送渠道" in res["reason"]
    assert len(logs) == 1
    assert logs[0][1] == "error"
    assert "not-configured" in logs[0][0]
    assert (notify._state().get("keys") or {}) == {}

    res2 = notify.send("复盘快照缺失", "正文", key="k0")
    assert res2["code"] == "not-configured"
    assert len(logs) == 1          # 去重：不重复刷 ERROR


def test_all_failed(notify_env, monkeypatch):
    """配置了渠道但全失败：code=all-failed、status.degraded=True、不记 key。"""
    _write_config(notify_env, {"channels": {"serverchan": {"sendkey": "placeholder"}}})
    monkeypatch.setattr(notify, "_send_serverchan", lambda *a, **k: (False, "stub"))
    logs = _capture_logs(monkeypatch)

    res = notify.send("t", "body", key="k0")
    assert res["sent"] is False
    assert res["code"] == "all-failed"
    assert res["channels"]["serverchan"]["ok"] is False
    assert logs and logs[-1][1] == "error"

    st = notify.status()                 # 真 status()，不替换
    assert st["degraded"] is True
    assert st["last_send"]["sent"] is False
    assert st["last_send"]["code"] == "all-failed"
    assert "k0" not in (notify._state().get("keys") or {})

    n = len(logs)
    notify.send("t", "body", key="k0")   # 同 code 1 小时内：不重复写 ERROR
    assert len(logs) == n


def test_ok(notify_env, monkeypatch):
    """真送达：code=ok、key 进 keys、degraded=False。"""
    _write_config(notify_env, {"channels": {"serverchan": {"sendkey": "placeholder"}}})
    monkeypatch.setattr(notify, "_send_serverchan", lambda *a, **k: (True, "stub"))
    _capture_logs(monkeypatch)

    res = notify.send("t", "body", key="k1")
    assert res["sent"] is True
    assert res["code"] == "ok"
    assert "k1" in notify._state()["keys"]
    st = notify.status()
    assert st["degraded"] is False
    assert st["last_send"]["sent"] is True


def test_status_unconfigured(notify_env):
    """无配置文件：degraded=True、last_send=None，原有四键齐全。"""
    st = notify.status()
    assert st["degraded"] is True
    assert st["last_send"] is None
    for k in ("configured", "channels", "min_interval_hours", "config_path"):
        assert k in st
    assert st["configured"] is False
    assert st["channels"] == []


# ------------------------------------------------------------- watchdog._recap_alert

def test_watchdog_marks_only_when_sent(monkeypatch):
    """sent=False 不写 recap_alert_date；同 (date, code) 只记一行；异常不留标记。"""
    logs = []
    monkeypatch.setattr(watchdog, "_recap_freshness_missing", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "_log", lambda msg, *a, **k: logs.append(msg))

    def _fake_notify(result):
        return types.SimpleNamespace(send=lambda *a, **k: result)

    today8 = time.strftime("%Y%m%d")

    # 未送达：不消耗当日额度，只写 last 摘要 + 一行日志
    monkeypatch.setitem(sys.modules, "notify", _fake_notify(
        {"sent": False, "code": "not-configured", "reason": "未配置推送渠道"}))
    st = {}
    watchdog._recap_alert(st)
    assert "recap_alert_date" not in st
    assert st["recap_alert_last"]["sent"] is False
    assert st["recap_alert_last"]["date"] == today8
    assert st["recap_alert_last"]["code"] == "not-configured"
    assert len(logs) == 1

    watchdog._recap_alert(st)            # 同一 (date, code)：不重复记日志
    assert len(logs) == 1

    # 真送达：写当日额度，清掉失败摘要
    monkeypatch.setitem(sys.modules, "notify", _fake_notify(
        {"sent": True, "code": "ok"}))
    watchdog._recap_alert(st)
    assert st["recap_alert_date"] == today8
    assert "recap_alert_last" not in st

    # send 抛异常：不写任何标记（冻结用例行为）
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(send=_boom))
    st2 = {}
    watchdog._recap_alert(st2)
    assert "recap_alert_date" not in st2
    assert "recap_alert_last" not in st2
