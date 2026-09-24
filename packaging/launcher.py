# -*- coding: utf-8 -*-
"""AK 看板 exe 启动器（PyInstaller 入口，onedir / onefile 两种打包形态共用）。

职责：
0. 桌面窗口模式（默认，见 gui.py）：非 .py 转发且未指定 --web 时，把
   server 放进后台线程、用 pywebview(WebView2) 开独立桌面窗口；起不来
   自动回退旧行为。--web/--no-gui 强制旧行为，--console 现场开一个控制台看日志。
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
    # exe 是窗口子系统（spec console=False）时没有控制台：os.system 拉起的 cmd
    # 会被 Windows 分配一个新控制台闪黑窗，必须先确认确实有控制台才切 65001。
    try:
        import ctypes
        if os.name == "nt" and not ctypes.windll.kernel32.GetConsoleWindow():
            return
    except Exception:
        pass
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


def _fallback_stdio(app):
    """窗口子系统构建（console=False）没有控制台，stdout/stderr 是 None（print
    静默丢弃）。--web 与 GUI 回退路径的旧行为靠控制台看日志，这里兜到文件里。
    只用于 server 模式；.py 转发（抓取子进程）的 stdout 是 server 重定向的日志
    句柄，绝不能覆盖。"""
    if sys.stdout is not None and hasattr(sys.stdout, "fileno"):
        return
    try:
        fh = open(os.path.join(os.environ.get("TEMP", app or "."),
                               "ak-server-fallback.log"),
                  "a", encoding="utf-8", buffering=1)
        sys.stdout = fh
        sys.stderr = fh
    except Exception:
        pass


GUI_OFF_FLAGS = ("--web", "--no-gui")   # 旧行为：server 自动开浏览器（日志见 fallback 文件）
GUI_ALL_FLAGS = GUI_OFF_FLAGS + ("--console",)   # --console：桌面窗口但保留控制台日志


def main():
    app = _app_root()
    argv = sys.argv[1:]
    script_mode = bool(argv) and argv[0].lower().endswith(".py")
    # 桌面窗口只接管「双击直接运行」的形态：.py 转发（计划任务/采集）与显式
    # --web 走旧路径，--console 照常进 GUI 但保留控制台。
    want_gui = (not script_mode
                and getattr(sys, "frozen", False)
                and os.name == "nt"
                and not any(f in argv for f in GUI_OFF_FLAGS))
    passthrough = [a for a in argv if a not in GUI_ALL_FLAGS]
    if want_gui:
        try:
            import gui
            gui.run(app, passthrough, keep_console="--console" in argv)
            return
        except Exception as exc:   # gui 导入失败 / GUIUnavailable
            try:
                import traceback
                with open(os.path.join(os.environ.get("TEMP", app or "."),
                                       "ak-gui-debug.log"), "a", encoding="utf-8") as fh:
                    fh.write("%s [launcher] gui fallback: %r\n%s" % (
                        __import__("time").strftime("%F %T"), exc,
                        traceback.format_exc()))
            except Exception:
                pass
            print(f"[看板] 桌面窗口不可用（{exc}），回退浏览器模式。")
    if script_mode:
        script = os.path.abspath(argv[0])
        sys.argv = [script] + argv[1:]
        sys.path.insert(0, os.path.dirname(script))
        runpy.run_path(script, run_name="__main__")
    else:
        server = os.path.join(app, "server.py")
        sys.argv = [server] + passthrough
        _fallback_stdio(app)
        runpy.run_path(server, run_name="__main__")


if __name__ == "__main__":
    _setup_console()
    main()
