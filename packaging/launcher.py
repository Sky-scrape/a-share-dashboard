# -*- coding: utf-8 -*-
"""AK 看板 exe 启动器（PyInstaller 入口，onedir / onefile 两种打包形态共用）。

职责：
1. 无参数（或 server.py 参数）：运行 server.py。
   - onedir：源码树就在 exe 旁 _internal（持久），server 的 ROOT 锚定它，照常工作。
   - onefile：运行时解压到临时目录（退出即删），数据不能落那里——首次启动把内嵌
     源码树同步到 %LOCALAPPDATA%\ak-dashboard（持久），server 的 ROOT 落在该目录，
     data/.status/复盘笔记随目录持久；按 exe 尺寸+mtime 做构建指纹，换新版 exe 自动
     重同步源码（只覆盖 web/backend/quant 与 server.py/start.py，用户数据永不动）。
2. 第一个参数以 .py 结尾：按脚本模式运行。打包后 sys.executable 是本 exe，而
   server.py 派生抓取子进程用的正是 [sys.executable, 脚本路径]（fetch_daily.py、
   ths_collect.py 等），不转发的话所有手动补抓/自愈拉起都会失败。onefile 下每个
   子进程自解压一次（数秒），抓取类任务分钟级时长，开销可接受。

控制台编码：抓取日志文件按 UTF-8 打开（server 侧 open(encoding="utf-8")），
子进程 stdout 直通该句柄，这里把 stdout/stderr 重配为 UTF-8，保证 exe 派生的
采集日志与 python 直跑逐字节一致；控制台先切 65001，横幅中文不乱码。
"""
import os
import runpy
import shutil
import sys

APP_DIR_NAME = "ak-dashboard"
SYNC_TREES = ("web", "backend", "quant")
SYNC_FILES = ("server.py", "start.py")


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


def _meipass():
    return getattr(sys, "_MEIPASS", None) \
        or os.path.dirname(os.path.abspath(__file__))


def _is_onefile():
    mp = getattr(sys, "_MEIPASS", None)
    if not mp:
        return False
    # onedir 的 _MEIPASS 是 <exe目录>/_internal；onefile 解压在系统临时目录
    return os.path.dirname(mp) != os.path.dirname(os.path.abspath(sys.executable))


def _app_root():
    """返回源码树与运行数据的持久根目录（server.py 所在，即 server 的 ROOT）。"""
    base = _meipass()
    if not _is_onefile():
        return base
    app = os.path.join(os.environ.get("LOCALAPPDATA")
                       or os.path.dirname(os.path.abspath(sys.executable)),
                       APP_DIR_NAME)
    os.makedirs(app, exist_ok=True)
    stamp = os.path.join(app, ".ak-build-stamp")
    key = ""
    try:
        st = os.stat(sys.executable)
        key = "%d-%d" % (st.st_size, int(st.st_mtime))
        with open(stamp, encoding="utf-8") as fh:
            if fh.read().strip() == key:
                return app   # 指纹一致：源码已是本构建，直接用（秒过）
    except OSError:
        pass
    for tree in SYNC_TREES:   # 只动自家源码树；data/.status/notes 不碰
        src = os.path.join(base, tree)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(app, tree)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    for f in SYNC_FILES:
        src = os.path.join(base, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(app, f))
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        pass
    return app


def main():
    app = _app_root()
    argv = sys.argv[1:]
    if argv and argv[0].lower().endswith(".py"):
        script = os.path.abspath(argv[0])
        sys.argv = [script] + argv[1:]
        sys.path.insert(0, os.path.dirname(script))
        runpy.run_path(script, run_name="__main__")
    else:
        server = os.path.join(app, "server.py")
        sys.argv = [server] + argv
        runpy.run_path(server, run_name="__main__")


if __name__ == "__main__":
    _setup_console()
    main()
