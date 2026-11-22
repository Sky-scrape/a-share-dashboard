# -*- coding: utf-8 -*-
"""统一告警推送（2026-09-15）· Server酱 / 企业微信 / Telegram 三渠道，全部可选。

动机（0914 事故复盘）：health/备源/新鲜度告警此前只落在看板弹窗与胶囊上——
「打开看板才知道」；数据链在凌晨/盘后失败时用户不在看板前，只能事后发现。
本模块把告警推到手机：health 监控线程（server.py）、17:05 采集链（fetch_task.bat
→ notify_once.py）、凌晨完成链（us_close_task.py）、样本外漂移（spec_validate）
共用同一出口。

设计边界：
- 尽力而为：任何渠道失败只写日志，绝不向调用方抛错——告警失败不能反过来打断
  采集/验证主链路。
- 节流去重：同一 key 默认 6 小时内只推一次（.status/notify_state.json 记录
  上次推送时刻），防止监控线程每 10 分钟重扫把同一故障刷成轰炸。
- 渠道配置在 .status/notify.json（gitignored，含密钥不入库），模板见
  backend/notify.example.json；未配置任何渠道时 send() 返回 code=not-configured
  并以 ERROR 级记日志（同 code 1 小时内去重），status() 回报 configured=False、
  degraded=True，健康面板与 /api/health 可见。
- HTTP 口径（Mimosa 门禁 / SSRF 三重边界）：目标主机限定白名单
  （sctapi.ftqq.com / qyapi.weixin.qq.com / api.telegram.org），强制 https，
  解析 IP 必须 is_global（阻私网/环回/链路本地/元数据端点；本地代理 fake-IP 段
  198.18.0.0/15 走 netguard.is_proxy_fakeip 豁免——2026-09-24 实证 is_global 会把
  该段误判非公网致推送全挂，与 us_market/_safe_get 同病灶，裁决见 netguard 模块头），
  禁用重定向。
"""
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.parse

import requests

import netguard  # noqa: E402  SSRF 防线单一来源（backend/netguard.py）
import logutil   # noqa: E402  统一 logging（backend/logutil.py）

os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
STATUS_DIR = os.path.join(PROJECT_ROOT, ".status")
CONFIG_PATH = os.path.join(STATUS_DIR, "notify.json")
STATE_PATH = os.path.join(STATUS_DIR, "notify_state.json")
LOG_PATH = os.path.join(STATUS_DIR, "logs", "notify.log")

# SSRF 白名单：推送渠道的固定主机（凭据全部走 params/path/data，不改变主机）
_ALLOWED_HOSTS = ("sctapi.ftqq.com", "qyapi.weixin.qq.com", "api.telegram.org")

DEFAULT_MIN_INTERVAL_H = 6.0   # 同一告警 key 的默认节流窗口
_TIMEOUT = 8                   # 单渠道推送超时（秒）：告警是尽力而为，不占主链路


def _log(msg, level="info"):
    """告警日志：stdout + 追加 .status/logs/notify.log（logutil 统一出口）。

    notify 被 server 巡检线程 / fetch_task / us_close_task / watchdog 多进程共用，
    notify.log 是跨进程聚合出口，故用 get_file_logger 复合 logger——notify 被
    哪个进程拉起、推没推成，一个文件看全（2026-09-27 并入 logutil，方案 O-2，
    此前为手写 open/append，时间戳格式与 stdout 双轨）。
    level：info/error 等 logging 级别名（未配置/全渠道失败走 error，面板与日志
    同权可见——告警通道本身坏掉也要出声）。"""
    logutil.get_file_logger("ak.notify", LOG_PATH).log(
        getattr(logging, level.upper(), logging.INFO), msg)


def _safe_float(v, default):
    """宽松数值解析：None/非数字（如 "6h"）/NaN/inf → default。

    2026-09-30 审查：配置 .status/notify.json 与状态 notify_state.json 都是
    gitignored 手编文件，写错类型不能在消费端炸——send() 契约「绝不抛错」
    （watchdog 失联告警裸调本模块，status() 还被 /api/health 直调），本模块
    内所有数值解析统一走这里（与 app_config 的归一口径一致）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _post(host, path, data=None, json_body=None, extra_url=""):
    """白名单主机的 https POST。返回 (requests.Response|None, 失败原因|None)。

    三重边界：主机白名单 → 解析 IP 全部 is_global（防 DNS rebinding 解到私网/
    元数据端点）→ 禁用重定向（防 30x 跳出白名单）。wecom 的 webhook URL 来自
    本地配置，extra_url 传入前已校验 scheme/hostname 与白名单一致。
    """
    if host not in _ALLOWED_HOSTS:
        return None, f"host {host} 不在推送白名单"
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return None, f"resolve failed: {type(e).__name__}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None, "resolve 到非法地址"
        if not ip.is_global and not netguard.is_proxy_fakeip(ip):
            return None, f"resolve 到非公网地址 {ip}，拒绝请求"
    url = extra_url or f"https://{host}{path}"
    try:
        return requests.post(url, data=data, json=json_body,
                             timeout=_TIMEOUT, allow_redirects=False), None
    except Exception as e:  # noqa: BLE001 - 网络异常统一按渠道失败处理
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def load_config():
    """读渠道配置（.status/notify.json）；缺失/损坏 → None（调用方按未配置降级）。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def status():
    """健康面板可见的推送配置摘要（不含任何密钥明文）。

    degraded：告警通道自身是否需要用户介入——未配置任何渠道，或最近一次
    真实发送全渠道失败且此后没有成功送达，置 True（/api/health 直调）。
    last_send：最近一次发送终态（含 code/reason/ts），供面板展示。
    """
    cfg = load_config() or {}
    ch = cfg.get("channels") or {}
    channels = []
    for name, c in ch.items():
        if not isinstance(c, dict):
            continue
        filled = {"serverchan": c.get("sendkey"),
                  "wecom": c.get("webhook"),
                  "telegram": c.get("bot_token") and c.get("chat_id")}.get(name)
        if filled:
            channels.append(name)
    try:
        last = _state().get("last") or None
    except Exception:  # noqa: BLE001 - 状态文件损坏不影响健康摘要
        last = None
    if not isinstance(last, dict):
        last = None
    configured = bool(channels)
    degraded = (not configured) or (
        bool(last) and last.get("sent") is False and last.get("code") == "all-failed")
    return {"configured": configured, "channels": channels,
            "min_interval_hours": _safe_float(
                cfg.get("min_interval_hours") or DEFAULT_MIN_INTERVAL_H,
                DEFAULT_MIN_INTERVAL_H),
            "config_path": CONFIG_PATH,
            "degraded": degraded,
            "last_send": last}


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _throttled(key, min_h):
    """同一 key 在 min_h 小时内已推过 → True。无 key（key=None）不节流。"""
    if not key:
        return False
    st = _state()
    last = _safe_float((st.get("keys") or {}).get(key) or 0, 0.0)
    return (time.time() - last) < min_h * 3600


def _mark(key):
    if not key:
        return
    st = _state()
    st.setdefault("keys", {})[key] = time.time()
    st["keys"] = {k: v for k, v in st["keys"].items()
                  if time.time() - _safe_float(v, 0.0) < 7 * 24 * 3600}   # 7 天外的记忆清理
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        from fsutil import save_json_atomic
        save_json_atomic(STATE_PATH, st)
    except Exception as e:  # noqa: BLE001 - 状态写失败只影响节流精度
        _log(f"[warn] state save failed: {type(e).__name__}: {str(e)[:120]}")


def _record_receipt(title, key, sent, code, reason):
    """记录最近一次发送终态，返回 "wrote" / "suppressed" / "error"。

    同一 code 在 1 小时内重复出现 → "suppressed"：不写盘也不额外刷日志，
    防止 watchdog 每 15 分钟巡检把「未配置/全渠道失败」刷成同一行轰炸。
    sent=False 的商业告警也一样去重；只有真送达才由 send() 走 _mark 记 key。
    保留 st["keys"]：先读 _state() 再只改 st["last"]，不覆盖节流记忆。
    """
    try:
        st = _state()
        if not isinstance(st, dict):
            st = {}
        prev = st.get("last") or {}
        if not isinstance(prev, dict):
            prev = {}
        if prev.get("code") == code and \
                time.time() - _safe_float(prev.get("ts") or 0, 0.0) < 3600:
            return "suppressed"
        st["last"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "ts": time.time(), "title": str(title)[:60],
                      "key": key or "", "sent": bool(sent), "code": code,
                      "reason": str(reason or "")[:200]}
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        from fsutil import save_json_atomic
        save_json_atomic(STATE_PATH, st)
        return "wrote"
    except Exception:  # noqa: BLE001 - 状态写失败只影响可见性，不影响发送结果
        return "error"


# ---------------------------------------------------------------- 渠道实现

def _send_serverchan(sendkey, title, text):
    # 字面量主机 + 白名单；sendkey 走路径段（用户本地配置的凭据）
    r, err = _post("sctapi.ftqq.com", f"/{sendkey}.send",
                   data={"title": title[:32], "desp": text[:30000]})
    if err:
        return False, err
    ok = r.status_code == 200 and (r.json() or {}).get("code") == 0
    return ok, f"sc rc={r.status_code}"


def _send_wecom(webhook, title, text):
    # webhook URL 来自本地配置：scheme/hostname 必须精确匹配白名单后才放行
    try:
        parts = urllib.parse.urlsplit(str(webhook))
    except ValueError:
        return False, "wecom webhook 非法"
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS \
            or parts.port not in (None, 443) \
            or (parts.username or parts.password):
        return False, "wecom webhook 必须 https://qyapi.weixin.qq.com/..."
    r, err = _post(parts.hostname, parts.path, json_body={
        "msgtype": "text", "text": {"content": f"{title}\n{text}"[:2000]}},
        extra_url=urllib.parse.urlunsplit(parts._replace(scheme="https")))
    if err:
        return False, err
    ok = r.status_code == 200 and (r.json() or {}).get("errcode") == 0
    return ok, f"wecom rc={r.status_code}"


def _send_telegram(bot_token, chat_id, title, text):
    # 字面量主机 + 白名单；bot_token 走路径段（用户本地配置的凭据）
    r, err = _post("api.telegram.org", f"/bot{bot_token}/sendMessage",
                   data={"chat_id": chat_id, "text": f"*{title}*\n{text}",
                         "parse_mode": "Markdown"})
    if err:
        return False, err
    ok = r.status_code == 200 and (r.json() or {}).get("ok") is True
    return ok, f"tg rc={r.status_code}"


def send(title, text="", key=None, min_interval_hours=None):
    """推送告警到全部已配置渠道。返回 {"sent": bool, "code": str, ...}；绝不抛错。

    key：节流指纹（如 "health:duckdb"）；同一 key 在节流窗口内只推一次。
    min_interval_hours：覆盖配置里的默认窗口（如复审类告警传 24）。
    code：ok=至少一个渠道真送达；not-configured=未配置渠道（ERROR 日志 + 1h 去重）；
    throttled=同 key 节流中（不写 last，不覆盖上次真实结果）；all-failed=全渠道失败。
    """
    st = status()
    if not st["configured"]:
        reason = "未配置推送渠道（.status/notify.json，模板 backend/notify.example.json）"
        if _record_receipt(title, key, False, "not-configured", reason) != "suppressed":
            _log(f"[error] send skipped key={key or '-'} code=not-configured：{reason}",
                 level="error")
        return {"sent": False, "code": "not-configured", "reason": reason}
    if _throttled(key, _safe_float(min_interval_hours or st["min_interval_hours"],
                                   st["min_interval_hours"])):
        return {"sent": False, "code": "throttled", "reason": "throttled"}
    text = text or ""
    results = {}
    ch = (load_config() or {}).get("channels") or {}
    any_ok = False
    for name in st["channels"]:
        c = ch.get(name) or {}
        try:
            if name == "serverchan":
                ok, info = _send_serverchan(c.get("sendkey"), title, text)
            elif name == "wecom":
                ok, info = _send_wecom(c.get("webhook"), title, text)
            elif name == "telegram":
                ok, info = _send_telegram(c.get("bot_token"), c.get("chat_id"), title, text)
            else:
                ok, info = False, "unknown channel"
        except Exception as e:  # noqa: BLE001 - 任何渠道异常都不外溢
            ok, info = False, f"{type(e).__name__}: {str(e)[:120]}"
        results[name] = {"ok": ok, "info": info}
        any_ok = any_ok or ok
    summary = " ".join(f"{k}={v['info']}" for k, v in results.items())
    code = "ok" if any_ok else "all-failed"
    rec = _record_receipt(title, key, any_ok, code, "" if any_ok else summary)
    if any_ok:
        _log(f"send key={key or '-'} ok=True " + summary)
        _mark(key)
    elif rec != "suppressed":   # 同 code 1 小时内已报过：不再刷 ERROR 日志
        _log(f"send key={key or '-'} ok=False " + summary, level="error")
    return {"sent": any_ok, "code": code, "channels": results}


# ---------------------------------------------------------------- 自检入口
# 动机（2026-10-16 C15 复审）：配置项填完后，此前只能等真实故障才知道通不通——
# 10-15 那次「复盘快照缺失」告警正是因渠道未配置而静默，事后才发现。
# 用法：python backend/notify.py          # 只看配置摘要与该填哪个字段
#       python backend/notify.py --send   # 实发一条测试推送（不节流），逐渠道回报
if __name__ == "__main__":
    import sys

    st = status()
    print("推送配置：", json.dumps(st, ensure_ascii=False))
    if not st["configured"]:
        print("\n未配置任何渠道 → send() 返回 sent=False 并记 ERROR"
              "（同 code 1h 去重），真实推送无法送达。请在 "
              f"{st['config_path']} 里填任意一个：")
        print("  channels.serverchan.sendkey   Server酱 sendkey（sctapi.ftqq.com，微信推送）")
        print("  channels.wecom.webhook        企业微信群机器人完整 webhook 地址")
        print("  channels.telegram.bot_token + .chat_id   Telegram Bot")
        print("填完执行：python backend/notify.py --send 验证是否真能送达。")
        sys.exit(2)
    if "--send" not in sys.argv:
        print("已配置渠道：%s（加 --send 可实发一条测试推送）" % ", ".join(st["channels"]))
        sys.exit(0)
    # key=None 绕过节流：测试推送不该被上一次告警的节流窗口吃掉
    res = send(title="看板告警自检",
               text=time.strftime("%Y-%m-%d %H:%M:%S")
               + " 这是一条测试推送。收到即说明告警链路已通，"
                 "后续复盘缺口/样本外退化/服务失联都会推到本渠道。")
    print("测试推送结果：", json.dumps(res, ensure_ascii=False))
    sys.exit(0 if res.get("sent") else 1)
