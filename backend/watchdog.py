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
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import logutil  # noqa: E402  统一 logging（backend/logutil.py）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATUS_DIR = os.path.join(ROOT, ".status")
STATE_PATH = os.path.join(STATUS_DIR, "watchdog.json")
LOG_PATH = os.path.join(STATUS_DIR, "logs", "watchdog.log")

LOGS_DIR = os.path.join(STATUS_DIR, "logs")
LOGS_KEEP_DAYS = 30         # 杂项日志保留天数（方案 O-4，2026-09-27）

FAILS_TO_ALERT = 2          # 连续失败 N 次才告警（单次失败可能是恰好重启窗口）
RESTART_COOLDOWN_S = 6 * 3600   # 自动拉起后 6 小时内不再重复拉起（防拉起风暴）

RECAP_DEADLINE = (18, 30)   # 交易日当天此刻后仍无当日快照即告警（17:05 任务 + 1.5h 余量）
RECAP_DIR = os.path.join(ROOT, "data", "recap")
ASHARE_CAL = os.path.join(RECAP_DIR, "us_market", "ashare_cal.json")


def _log(msg):
    """看门狗日志：stdout（.bat 重定向照旧）+ 追加 .status/logs/watchdog.log。

    看门狗是 15 分钟一次的无状态单次运行，watchdog.log 是跨次运行的连续叙事
    （连续失败计数在状态文件，过程在日志）；2026-09-27 并入 logutil 统一出口
    （方案 O-2），此前为手写 print+open 双写。"""
    logutil.get_file_logger("ak.watchdog", LOG_PATH).info(msg)


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


def _local_trade_dates8():
    """本地 A 股日历（data/recap/us_market/ashare_cal.json）的 8 位交易日集合。

    刻意不触网：看门狗每 15 分钟跑一次，走 hithink 网络日历会把「探活」本身
    变成网络依赖（网络断时探活脚本自己先卡住）。解析口径与 backend/trade_cal.py
    的本地兜底同源；日历缺失/损坏/为空返回 None。
    """
    try:
        with open(ASHARE_CAL, encoding="utf-8") as f:
            dates = json.load(f).get("dates") or []
    except Exception:  # noqa: BLE001
        return None
    d8 = {re.sub(r"\D", "", str(x)) for x in dates}
    d8 = {d for d in d8 if len(d) == 8}
    return d8 or None


def _recap_freshness_missing(today8, hhmm, local_dates, recap_dir):
    """纯函数：最近一个「应当已有快照」的交易日是否缺快照（True=该告警）。

    判定的是「最近一个已收盘的交易日」而非死盯今天——机器 17:05 前休眠、当晚
    关机、次日才开机时，缺口仍要在开机后第一轮巡检里被看到（10-15 缺口正是当晚
    无人知情、次日 04:05 才被诊断出来）。

    告警门槛取「宁可漏报也不误报」：本地日历不可用、或日历未覆盖今日（日历陈旧，
    无法判断今天算不算交易日）一律不告警；未到 RECAP_DEADLINE 时今天不算候选
    （17:05 任务窗口内抓取可能正在进行）；候选集为空（今日之前无交易日）不告警。
    """
    if not local_dates:
        return False
    if not (min(local_dates) <= today8 <= max(local_dates)):
        return False                        # 日历陈旧未覆盖今日：不下结论
    cands = [d for d in local_dates if d < today8]
    if (hhmm[0], hhmm[1]) >= RECAP_DEADLINE and today8 in local_dates:
        cands.append(today8)                # 今日是交易日且已过截止时刻：今日也该有
    if not cands:
        return False
    target = max(cands)                     # 最近一个应有快照的交易日
    return not (os.path.exists(os.path.join(recap_dir, target + ".json"))
                or os.path.exists(os.path.join(recap_dir, target + ".json.gz")))


def _recap_alert(st):
    """交易日复盘快照缺失告警（每交易日一次；独立于 17:05 采集链本身）。

    2026-10-16 补 10-15 缺口事故：抓取进程被系统级中断（S0 待机断网/关机）时
    静默消失——无 traceback、无 fetch end、快照未落盘，fetch_task.bat 尾部的
    失败推送根本没机会执行；凌晨 usclose 链虽能如实诊断，却因 .status/notify.json
    未配置而静默。看门狗是唯一「独立于采集链」的常驻探针，在此兜住这条路径。
    """
    today8 = time.strftime("%Y%m%d")
    if st.get("recap_alert_date") == today8:   # 同日只报一次（状态跨次运行接力）
        return
    lt = time.localtime()
    if not _recap_freshness_missing(today8, (lt.tm_hour, lt.tm_min),
                                    _local_trade_dates8(), RECAP_DIR):
        return
    # 告警本身绝不允许反噬探活：本函数在 run_once 的两个分支之前调用，抛出就会
    # 连 watchdog.json 一起丢掉——服务失联计数归零、连败阈值永远到不了。历史上
    # 「bash 用裸 python 解到 LibreOffice 解释器、import notify 炸掉」已让整条
    # 告警链静默失效数天（见 watchdog_task.bat 的 data-2 注释），这里显式兜住。
    try:
        import notify
        notify.send(
            title="复盘快照缺失",
            text=(f"{time.strftime('%Y-%m-%d %H:%M')}：最近一个交易日（{today8} 或更早）"
                  "的复盘快照不存在——17:05 采集链失败，或机器休眠/关机错过了任务。\n"
                  "补齐：看板复盘页点『重新抓取』，或运行 "
                  "python backend/recap/fetch_daily.py（缺 T 日备选池再跑 "
                  "python backend/recap/us_close_task.py）。\n"
                  "（若本条一直没收到，是 .status/notify.json 尚未配置推送渠道。）"),
            key=f"watchdog:recap-missing:{today8}", min_interval_hours=12)
        st["recap_alert_date"] = today8
    except Exception as e:  # noqa: BLE001 - 探活链路优先级高于告警
        _log(f"[warn] 复盘缺口告警发送失败（不影响探活）: {type(e).__name__}: {e}")


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


def prune_logs(keep_days=None):
    """.status/logs 杂项日志按 mtime 清理超期文件（方案 O-4，2026-09-27）。

    server.log 由 start.py/gui.py 启动时轮转改名（server.log.<时间戳>），改名件与
    backfill/debug 等杂项日志此前无限累积（实测 50+ 个文件）。看门狗每 15 分钟
    单次运行，顺带清一次：mtime 早于 keep_days 天的删除；子目录（archive/）与
    删除失败（Windows 上正被写/被占用的日志）静默跳过——清理是尽力而为，绝不
    反过来影响探活主链路。保留天数可用 .status/config.json 的 logs_keep_days
    覆盖（backend/app_config.py，方案 S-5）。"""
    if keep_days is None:
        try:
            import app_config   # noqa: PLC0415  运营常量薄层（backend/app_config.py）
            keep_days = app_config.get("logs_keep_days")
        except Exception:  # noqa: BLE001 - 配置层故障回默认，清理照跑
            keep_days = LOGS_KEEP_DAYS
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        names = os.listdir(LOGS_DIR)
    except OSError:
        return removed
    for name in names:
        p = os.path.join(LOGS_DIR, name)
        if not os.path.isfile(p):
            continue
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


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
    # 数据缺口与服务的死活无关：无论探活成败都查一次（缺口只能靠这个独立探针发现）
    _recap_alert(st)
    if ok:
        if st.get("consecutive_fails"):
            _log(f"恢复：服务已响应（此前连续失败 {st['consecutive_fails']} 次）")
        # 2026-09-30 修：成功分支改 update（与失败分支同一模式）——此前用全新
        # dict 整体覆写，把用户写在同一文件里的 autostart=true 静默抹掉，自愈
        # 拉起在首次健康探测后必然自关（告警文案还引导用户去写这个文件）。
        st.update({"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "consecutive_fails": 0})
        _save_state(st)
        _backup_alert()
        prune_logs()
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
