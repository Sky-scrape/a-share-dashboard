# -*- coding: utf-8 -*-
"""看板看门狗（2026-09-15）· 服务失联探测 + 数据链失败兜底推送。

动机：health 告警依赖 server 进程活着；服务本身静默死亡时（runbook 里
「手机打不开常因服务已停」）没有任何东西能报信——死人不能喊救命。本脚本
由计划任务 arecap-watchdog 每 15 分钟拉起一次（无状态单次运行）：

  1. GET http://127.0.0.1:<port>/api/health（只探回环，8s 超时）；
  2. 服务正常 → 记录 .status/watchdog.json 后退出；顺带巡检 backup_state
     （backup.ok=false 或超 3 天没备份 → 以独立 key 推送，与备份脚本解耦）；
  3. 连续 ≥2 次（约 30 分钟）探不通 → 推送「看板服务失联」；配置了
     autostart=true 时顺带把 start.py 拉起（默认 false：用户主动关服务的
     意图不应被对抗，只告警不打扰）。

连续失败计数就存在 watchdog.json 里（进程无状态，跨次运行靠文件接力）。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATUS_DIR = os.path.join(ROOT, ".status")
STATE_PATH = os.path.join(STATUS_DIR, "watchdog.json")
LOG_PATH = os.path.join(STATUS_DIR, "logs", "watchdog.log")

FAILS_TO_ALERT = 2          # 连续失败 N 次才告警（单次失败可能是恰好重启窗口）
RESTART_COOLDOWN_S = 6 * 3600   # 自动拉起后 6 小时内不再重复拉起（防拉起风暴）


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        sys.path.insert(0, os.path.dirname(HERE))
        from fsutil import save_json_atomic
        save_json_atomic(STATE_PATH, st)
    except Exception as e:  # noqa: BLE001
        _log(f"[warn] state save failed: {type(e).__name__}")


def _probe(port):
    """只探本机回环（SSRF 白名单口径，与 start.py peer 探测同一套边界：
    端口显式校验 → 字面量 127.0.0.1/http → 解析 IP 全部必须环回 → 禁重定向）。"""
    import ipaddress
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not (1024 <= port <= 65535):
        return False
    url = "http://127.0.0.1:%d/api/health" % port
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        return False
    try:
        for info in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP):
            if not ipaddress.ip_address(info[4][0]).is_loopback:
                return False   # 解析到非环回地址：拒绝探测
    except (OSError, ValueError):
        return False

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d.get("service") == "ak-dashboard"
    except Exception:  # noqa: BLE001 - 连不上/非本服务都算失联
        return False


def _backup_alert():
    """备份巡检：失败或超过 3 天没成功备份 → 独立 key 推送（节流由 notify 管）。"""
    try:
        with open(os.path.join(STATUS_DIR, "backup_state.json"), encoding="utf-8") as f:
            bs = json.load(f)
    except Exception:  # noqa: BLE001
        bs = {}
    stale_days = None
    if bs.get("at"):
        try:
            t = time.mktime(time.strptime(bs["at"], "%Y-%m-%d %H:%M:%S"))
            stale_days = (time.time() - t) / 86400
        except Exception:  # noqa: BLE001
            pass
    bad = (bs.get("ok") is False) or (stale_days is not None and stale_days > 3)
    if not bad:
        return
    import notify
    notify.send(title="数据备份异常",
                text=(f"backup.ok={bs.get('ok')} 最近成功备份={bs.get('at') or '无'}"
                      f"（{f'{stale_days:.1f}' if stale_days is not None else '-'} 天前）"
                      f"\n错误：{bs.get('error') or '-'}\n目标目录：{bs.get('dest') or '-'}"
                      "\n请检查磁盘空间/目录权限，或手动运行 python backend/backup.py"),
                key="backup:abnormal", min_interval_hours=12)


def _maybe_restart(cfg):
    """autostart 配置开启且冷却已过 → 后台拉起 start.py（服务级自愈）。

    默认不开启：看板可能被用户有意关闭（如清理环境），自动拉起会对抗用户
    意图；开启后本函数也只拉 start.py——它自带监护循环与端口身份校验。"""
    if not (cfg or {}).get("autostart"):
        return False
    st = _load_state()
    last_boot = float(st.get("last_restart") or 0)
    if time.time() - last_boot < RESTART_COOLDOWN_S:
        return False
    py = sys.executable
    try:
        subprocess.Popen(
            [py, os.path.join(ROOT, "start.py"), "--no-browser"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0))
        st = _load_state()
        st["last_restart"] = time.time()
        _save_state(st)
        _log("autostart: start.py 已拉起")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"[warn] autostart failed: {type(e).__name__}: {str(e)[:120]}")
        return False


def run_once(port, config_path=None):
    cfg = {}
    try:
        with open(config_path or os.path.join(STATUS_DIR, "watchdog.json"),
                  encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:  # noqa: BLE001 - 无配置=默认口径（只告警不拉起）
        pass
    st = _load_state()
    ok = _probe(port)
    if ok:
        if st.get("consecutive_fails"):
            _log(f"恢复：服务已响应（此前连续失败 {st['consecutive_fails']} 次）")
        _save_state({"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "consecutive_fails": 0,
                     "last_restart": st.get("last_restart")})
        _backup_alert()
        return 0

    fails = int(st.get("consecutive_fails") or 0) + 1
    st.update({"ok": False, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "consecutive_fails": fails,
               "last_restart": st.get("last_restart")})
    _save_state(st)
    _log(f"服务失联（连续第 {fails} 次）")
    if fails >= FAILS_TO_ALERT:
        restarted = _maybe_restart(cfg)
        import notify
        notify.send(
            title="看板服务失联",
            text=(f"http://127.0.0.1:{port}/api/health 连续 {fails} 次探测失败"
                  f"（约 {fails * 15} 分钟）。\n"
                  + (f"已按配置自动拉起 start.py，请稍后验证。"
                     if restarted else
                     "未开启自动拉起（.status/watchdog.json 里 autostart=true 可开启）。")
                  + "\n手机打不开看板通常就是这个原因；恢复后请手动运行 打开看板.bat 核对。"),
            key="watchdog:down", min_interval_hours=3)
    return 1


def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="看板看门狗（单次运行，计划任务每 15 分钟拉起）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AK_PORT") or 8000))
    args = ap.parse_args()
    return run_once(args.port)


if __name__ == "__main__":
    sys.exit(main())
