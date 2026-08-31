# -*- coding: utf-8 -*-
"""
一键启动 A股看板（两个板块）：
  板块① 日内轮动   http://127.0.0.1:8000/  （本目录：大盘可视化）
  板块② 盘后复盘   http://127.0.0.1:8001/  （上级目录 arecap/，见下方 RECAP_DIR）

用法:
    python start.py
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
RECAP_DIR = os.path.join(os.path.dirname(ROOT), "arecap")
PORT_MAIN = 8000
PORT_RECAP = 8001


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def main():
    procs = []

    if port_in_use(PORT_RECAP):
        print(f"[复盘] 端口 {PORT_RECAP} 已有服务，跳过启动")
    else:
        print(f"[复盘] 启动 arecap 看板 -> :{PORT_RECAP}")
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(RECAP_DIR, "backend", "serve.py"),
             "--port", str(PORT_RECAP), "--no-open"],
            cwd=RECAP_DIR,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ))

    if port_in_use(PORT_MAIN):
        print(f"[轮动] 端口 {PORT_MAIN} 已有服务，跳过启动")
    else:
        print(f"[轮动] 启动日内轮动看板 -> :{PORT_MAIN}")
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server.py")],
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ))

    ok_main = wait_port(PORT_MAIN)
    ok_recap = wait_port(PORT_RECAP)
    if not ok_main:
        print("!! 日内轮动服务未能就绪，请检查端口占用或错误日志")
    if not ok_recap:
        print("!! 复盘看板服务未能就绪，请确认 arecap 目录存在或端口占用")

    url = f"http://127.0.0.1:{PORT_MAIN}/"
    print(f"打开看板: {url}")
    webbrowser.open(url)
    print("服务已在后台运行。关闭看板窗口不影响服务；")
    print("如需停止：任务管理器结束 python 进程，或关闭本窗口后执行 taskkill /IM python.exe /F")

    # 保持前台进程存活（Ctrl+C 结束）
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("停止启动器（服务仍在后台运行）")


if __name__ == "__main__":
    main()
