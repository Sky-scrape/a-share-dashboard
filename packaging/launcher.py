# -*- coding: utf-8 -*-
"""AK 看板 exe 启动器（PyInstaller 入口）。

职责只有一个：把「exe 调用」翻译成「python 调用」——

1. 无参数（或 server.py 参数）：运行内嵌的 server.py（onedir 版解包在
   sys._MEIPASS，即 dist/ak-dashboard/_internal/，web/ backend/ quant/ 源码
   都在其中，server.py 的 ROOT 逻辑照常成立）。
2. 第一个参数以 .py 结尾：按脚本模式运行它。打包后 sys.executable 是本 exe，
   而 server.py 派生抓取子进程用的正是 [sys.executable, 脚本路径]
   （如 backend/recap/fetch_daily.py、backend/rotation/ths_collect.py），
   不在这里转发的话，所有手动补抓/自愈拉起都会把脚本路径当成未知参数而失败。

控制台编码：抓取日志文件按 UTF-8 打开（server 侧 open(encoding="utf-8")），
子进程 stdout 直通该句柄，这里把 stdout/stderr 重配为 UTF-8，保证 exe 派生的
采集日志与 python 直跑逐字节一致；控制台先切 65001，横幅中文不乱码。
"""
import os
import runpy
import sys


def _setup_console():
    try:
        os.system("chcp 65001 >nul")
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    base = getattr(sys, "_MEIPASS", None) \
        or os.path.dirname(os.path.abspath(__file__))
    argv = sys.argv[1:]
    if argv and argv[0].lower().endswith(".py"):
        script = os.path.abspath(argv[0])
        sys.argv = [script] + argv[1:]
        sys.path.insert(0, os.path.dirname(script))
        runpy.run_path(script, run_name="__main__")
    else:
        server = os.path.join(base, "server.py")
        sys.argv = [server] + argv
        runpy.run_path(server, run_name="__main__")


if __name__ == "__main__":
    _setup_console()
    main()
