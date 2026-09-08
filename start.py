# -*- coding: utf-8 -*-
"""A股看板一键启动（统一服务，单端口，默认 8000，可用环境变量 AK_PORT 改）。

用法:
    python start.py
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server.py")
PORT = int(os.environ.get("AK_PORT") or 8000)   # 与 server.py 默认值同一环境变量，单一来源
LOG_DIR = os.path.join(ROOT, ".status", "logs")
SERVER_LOG = os.path.join(LOG_DIR, "server.log")

sys.path.insert(0, os.path.join(ROOT, "backend"))

try:
    from landing import landing_path  # 与 server.py 同一口径，不另写一套时间判断
except Exception:  # noqa: BLE001  启动器不能因为一个辅助模块报错就打不开
    landing_path = lambda *a, **k: "/"  # noqa: E731


def rotate_server_log():
    """启动前轮转：旧 server.log 改名为 server.log.<旧文件时间戳>，避免新旧日志混读。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(SERVER_LOG):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(SERVER_LOG)))
        try:
            os.replace(SERVER_LOG, os.path.join(LOG_DIR, f"server.log.{stamp}"))
        except OSError:  # 改名失败不阻塞启动，降级为覆盖写
            pass
    return open(SERVER_LOG, "a", encoding="utf-8")


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def peer_is_our_service(port):
    """端口占用时验证对端是不是本看板（/api/health 的 service 标记，2026-09-04 起）。

    旧版只看端口通不通就开浏览器：若 8000 被别的程序占用，用户会看到那个程序的
    404/报错页还以为是看板坏了。返回 True=本服务（可能是已在跑的旧实例）、
    False=别的程序、None=探测失败（按未知处理）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return (json_marker(r.read())) == "ak-dashboard"
    except Exception:  # noqa: BLE001
        return None


def json_marker(raw):
    try:
        import json
        return (json.loads(raw.decode("utf-8")) or {}).get("service")
    except Exception:  # noqa: BLE001
        return None


def precheck_deps():
    """启动前依赖预检（2026-09-04）：缺包直接给出可行动提示，而不是让 server
    在 import 时秒死、靠监护循环反复退避重启还查不到原因。
    返回是否全部就绪。"""
    ok = True
    for mod in ("requests", "numpy", "pandas"):
        try:
            __import__(mod)
        except ImportError:
            print(f"!! 缺少依赖 {mod}：请先运行  pip install -r requirements.txt")
            ok = False
    try:
        import shutil
        if not (shutil.which("hithink-finance") or shutil.which("hithink-finance.cmd")):
            print("!! 未找到 hithink-finance CLI（全站主力数据源）：请先  npm install -g hithink-finance")
            print("!!   （看板仍可启动，但复盘/竞价/轮动的抓取都会失败）")
    except Exception:  # noqa: BLE001
        pass
    return ok


def lan_ip():
    """UDP 套接字技巧拿出口网卡 IP（不真正发包）；离线/无路由返回 None。
    服务已绑 0.0.0.0：手机同 Wi-Fi 或经 Tailscale 虚拟内网访问时用（2026-09-03）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def wait_port(port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def spawn_server(logf):
    """拉起 server 子进程。-u 无缓冲：崩溃 traceback 能落盘可查（2026-09-03 静默死亡事故后加）。"""
    return subprocess.Popen(
        [sys.executable, "-u", SERVER, "--host", "0.0.0.0", "--no-open"],
        cwd=ROOT,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main():
    landing = landing_path()
    proc = None
    logf = None
    if not precheck_deps():
        sys.exit(1)
    if port_in_use(PORT):
        peer = peer_is_our_service(PORT)
        if peer is False:
            print(f"!! 端口 {PORT} 已被其他程序占用（不是本看板），退出。")
            print("!!   可用别的端口：  set AK_PORT=8010 && python start.py")
            sys.exit(1)
        print(f"[看板] 端口 {PORT} 已有服务在跑（身份校验{'通过' if peer else '未确认，若页面异常请检查端口占用'}），直接打开浏览器。")
        print("[看板] 提示：若页面功能异常（如接口 404），说明旧进程代码过老，")
        print("[看板] 请先结束旧 python 进程（任务管理器搜 python），再重新运行本脚本。")
    else:
        print(f"[看板] 启动统一服务 -> http://127.0.0.1:{PORT}{landing}")
        # --no-open：浏览器只由本启动器在端口就绪后开一个标签，不与 server 重复弹
        logf = rotate_server_log()
        # server 的 print 全部落到 .status/logs/server.log，崩溃输出可回查；
        # 子进程持有自己的句柄，本启动器退出不影响其继续写。
        proc = spawn_server(logf)

    if not wait_port(PORT):
        print("!! 服务未能就绪，请检查端口占用或错误日志")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}{landing}"
    print(f"看板已启动: {url}")
    if landing == "/auction":
        print("[看板] 现在是集合竞价时段，先打开实时竞价页；其他板块顶栏可切，"
              f"轮动页直接访问 http://127.0.0.1:{PORT}/")
    _lan = lan_ip()
    if _lan:
        print(f"[看板] 手机访问: http://{_lan}:{PORT}/ （同一 Wi-Fi）或 Tailscale 100.x 地址（任意网络）")
    webbrowser.open(url)
    print("服务在后台运行。关闭本窗口不影响服务；")
    if proc is not None:
        print("[看板] 监护已启用：服务进程若退出将自动重启（2026-09-03）")
    print("如需停止：任务管理器结束 python 进程")

    # 监护循环（2026-09-03）：server 曾多次静默死亡且无人拉起，导致手机/桌面一起打不开。
    # 启动器活着期间，子进程退出即重启；连续秒死时指数退避防崩溃空转。
    # （2026-09-04 修：端口已被外部占用时本循环不再空转——上面已 sys.exit 或 proc=None
    # 且无子进程可监护，直接待用户 Ctrl+C。）
    backoff = 3
    started_at = time.time()
    try:
        while proc is not None:
            rc = proc.poll()
            if rc is not None:
                uptime = time.time() - started_at
                backoff = 3 if uptime >= 15 else min(backoff * 2, 60)
                msg = (f"[supervisor] {time.strftime('%F %T')} 服务进程退出"
                       f"（code={rc}，存活 {int(uptime)}s），{backoff}s 后自动重启\n")
                print(msg, end="")
                try:
                    logf.write(msg)
                    logf.flush()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(backoff)
                logf = open(SERVER_LOG, "a", encoding="utf-8")
                proc = spawn_server(logf)
                started_at = time.time()
                if not wait_port(PORT, timeout=30):
                    print("!! 重启后端口仍未就绪，继续监护重试")
            time.sleep(2)
        while True:      # proc is None（端口被外部占用复用场景）：等用户 Ctrl+C
            time.sleep(2)
    except KeyboardInterrupt:
        print("停止启动器（服务仍在后台运行）")


if __name__ == "__main__":
    main()
