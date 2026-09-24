# -*- coding: utf-8 -*-
"""AK 看板桌面窗口前端（packaging/gui.py，随 exe 打包；开发态 python server.py 不经过这里）。

目标：双击 exe 得到一个独立桌面窗口（任务栏自己的图标、关窗即整个程序退出），
不再弹浏览器。实现：server.py 原样在进程内后台线程运行（同一 argparse、同一
端口/落点逻辑，一行不改），pywebview 用系统自带的 WebView2（Edge 内核）开
窗口指向 127.0.0.1；窗口关闭 = taskkill 本进程树（server 派生的抓取子进程
一并退出）。web/quant 等页面里的外链（公告/研报 target=_blank）交系统浏览器。
启动感知提速：自起服务时窗口先显示内联启动页（毫秒级），服务就绪后自动导航
进真实页面——窗口可见不再被 server 重导入（pandas 等 2-3s）阻塞。

单一来源：端口探测、服务身份校验（/api/health 的 service 标记）、server 日志
轮转，直接用 importlib 按文件加载随包的 start.py——start.py 在 spec 里是 data
而非 PYZ 模块，按文件加载才能和 python 直跑共用同一份逻辑。

降级链：pywebview / WebView2 runtime 不可用 → 抛 GUIUnavailable（先把被藏的
控制台还原），launcher 回退旧行为（控制台 + server 自动开浏览器）。

端口：默认 8000（AK_PORT / --port 与 server 同一口径）。被占用时：对端是本
看板（身份校验通过）→ 附身开窗（第二个窗口共享已跑的服务，关窗不影响对方）；
是别的程序 → 向后依次找空端口。默认绑 127.0.0.1（不触发防火墙授权弹窗）；
手机/Tailscale 访问照旧走 start.py 流程，或 --host 0.0.0.0。

开关：--web / --no-gui = 旧行为（server 自动开浏览器，日志兜到
%TEMP%\\ak-server-fallback.log）；--console = 桌面窗口但现场开控制台看日志；
AK_WEBVIEW_DEBUG=1 开 WebView2 devtools；AK_GUI_SMOKE=1
冒烟模式（窗口加载后自动关闭退出，供打包自检）。

标题栏随主题：Windows 系统标题栏默认黑/白与页面主题（晨报纸色/夜台深底）
不搭，用 DWM 属性把标题栏染成当前 --bg-0（Win11 支持任意色，Win10 退深浅
开关，更旧系统忽略、观感退回系统默认）。页面经 js_api 把 data-theme 推给
Python，页内切主题即时跟随；色值与 web/lib/tokens.css 单一来源保持一致。
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

GEO_REL = os.path.join(".status", "gui-window.json")   # 窗口尺寸/位置（随用户数据持久）
STORAGE_REL = os.path.join(".status", "webview")       # WebView2 localStorage 主题/自选持久化
DEFAULT_SIZE = (1440, 900)
MIN_SIZE = (960, 620)

# 启动页：自起服务时窗口先加载这段内联 HTML（毫秒级），服务后台就绪后再导航到
# 真实页面——「双击到窗口可见」不再被 server 启动（pandas 等重导入，约 2-3s）
# 和 onefile 自解压之后的剩余流程阻塞。
_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>AK 看板</title>
<style>
  html,body{margin:0;height:100%;background:#101418;color:#d7dde3;
    font-family:"Microsoft YaHei",system-ui,sans-serif;
    display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
  .spin{width:34px;height:34px;border-radius:50%;
    border:3px solid #2c343d;border-top-color:#4f8cff;animation:s .9s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
  b{font-size:17px;font-weight:600;letter-spacing:1px}
  small{color:#7c8792}
</style></head><body>
<div class="spin"></div><b>AK 看板</b><small>正在启动服务，马上就好…</small>
</body></html>"""


class GUIUnavailable(RuntimeError):
    """桌面窗口起不来（缺 pywebview / WebView2 runtime 等），调用方应回退浏览器模式。"""


_DEBUG_LOG = os.path.join(os.environ.get("TEMP", os.getcwd()), "ak-gui-debug.log")


def _trace(msg):
    """面包屑调试：GUI 链路每个阶段写一行到 %TEMP%\\ak-gui-debug.log。
    控制台可能被藏/被重定向，这是唯一可靠可查的出口。"""
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s [gui] %s\n" % (time.strftime("%F %T"), msg))
    except Exception:
        pass


# ---------- 控制台 ----------
# exe 是窗口子系统（spec console=False）：双击启动根本没有终端，无需藏窗；
# _hide_console 仅对旧 console 构建兜底（藏本程序独占的黑窗）。--console 则用
# AllocConsole 现场开一个控制台接回标准流。

_hidden = None   # 被藏的控制台 HWND，失败回退时还原


def _hide_console():
    """只藏本程序独占的控制台（console=True 旧构建双击启动时的黑窗）；从
    cmd/PowerShell 里手跑时控制台是共享的，藏了会把用户整个终端窗口藏掉，
    必须不动。窗口子系统构建下 GetConsoleWindow() 恒为 0，天然跳过。
    （GetConsoleWindow/GetConsoleProcessList 是 kernel32 API；ShowWindow 才是 user32）"""
    global _hidden
    if os.name != "nt":
        return
    try:
        import ctypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        buf = (ctypes.c_uint * 16)()
        n = k32.GetConsoleProcessList(buf, 16)
        if n != 1:   # ≥2 = 与终端共享（attach 模式），藏窗口会误伤用户的终端
            return
        u32.ShowWindow(hwnd, 0)   # SW_HIDE
        _hidden = hwnd
    except Exception as exc:   # 藏不住黑窗只是观感问题，绝不能挡住 GUI 本身
        _trace("hide console skipped: %r" % exc)


def _ensure_console():
    """console=False 构建下 --console：现场分配控制台并把标准流接回 CONOUT$，
    日志照看；已有控制台（python 直跑 / 旧构建）则不动。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if k32.GetConsoleWindow() or not k32.AllocConsole():
            return
        for name in ("stdout", "stderr"):
            try:
                setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                        buffering=1, errors="replace"))
            except Exception:
                pass
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except Exception:
            pass
        _trace("console allocated for --console")
    except Exception as exc:
        _trace("ensure console skipped: %r" % exc)


def _restore_console():
    if _hidden is not None and os.name == "nt":
        import ctypes
        ctypes.windll.user32.ShowWindow(_hidden, 5)   # SW_SHOW


class _Tee:
    """--console 模式：stdout 同时进控制台和 server.log，两不误。"""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, b):
        for s in self._streams:
            try:
                s.write(b)
            except Exception:
                pass
        return len(b)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


# ---------- 随包 start.py / server.py 的加载 ----------

def _load_start(root):
    """按文件加载随包 start.py（端口探测/身份校验/日志轮转的单一来源）。
    exec 期间它自己会把 backend/ 插进 sys.path 并带 landing 兜底。"""
    path = os.path.join(root, "start.py")
    if not os.path.isfile(path):
        raise GUIUnavailable(f"找不到 {path}")
    spec = importlib.util.spec_from_file_location("ak_dashboard_start", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _argval(argv, name):
    """取 --name value / --name=value 形式的参数值，取不到返回 None。"""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    prefix = name + "="
    for a in argv:
        if a.startswith(prefix):
            return a.split("=", 1)[1]
    return None


# ---------- 窗口几何 ----------

def _load_geo(root):
    try:
        with open(os.path.join(root, GEO_REL), encoding="utf-8") as fh:
            g = json.load(fh)
        kw = {
            "width": max(400, min(int(g.get("w", 0)), 7680)),
            "height": max(300, min(int(g.get("h", 0)), 4320)),
        }
        x, y = g.get("x"), g.get("y")
        if isinstance(x, int) and isinstance(y, int):
            kw["x"], kw["y"] = x, y
        return kw
    except Exception:
        return {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1]}


def _save_geo(root, win):
    try:
        p = os.path.join(root, GEO_REL)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"w": win.width, "h": win.height, "x": win.x, "y": win.y}, fh)
    except Exception:
        pass   # 记不住窗口位置不构成错误


# ---------- 标题栏随主题（DWM 染色，仅 Windows） ----------
# 系统标题栏默认黑/白与页面主题（晨报纸色/夜台深底）不搭。DWM 属性可改标题栏：
# DWMWA_USE_IMMERSIVE_DARK_MODE（Win10 20H1+，只切深浅）、DWMWA_CAPTION_COLOR /
# DWMWA_TEXT_COLOR（Win11 22000+，任意色）；旧系统属性调用报错被忽略，观感退回
# 系统默认。色值与 web/lib/tokens.css 的 --bg-0/--text-1 保持一致，改动需两处同步。
# 整条链路全部 try/except：染色失败只是观感问题，绝不能挡住窗口本身。

_TITLEBAR_COLOR = {   # data-theme 值 → (标题栏底色, 文字色, 深色按钮区)
    "paper":  ("#EFEAE0", "#21201C", False),   # 晨报纸色
    "cyber":  ("#070B14", "#D7E3FF", True),    # 夜台
    "splash": ("#101418", "#D7DDE3", True),    # 内联启动页（无 data-theme，初始深底）
}
_titlebar_now = ["splash"]   # 当前应刷的主题（最新一次调用为准）
_titlebar_hwnds = []         # 已定位的顶层窗口句柄缓存（跨导航复用，失效则重找）

# 页面 → Python 的桥：每次导航落地注入（JS 上下文随导航重建）。启动页无
# data-theme 不上报；真实页推 data-theme，并挂 akthemechange 让页内切换即时跟随。
_BRIDGE_JS = """(function(){
  function push(){
    var t = document.documentElement.getAttribute('data-theme');
    if (!t) return;
    var api = window.pywebview && window.pywebview.api;
    if (api && api.set_titlebar) {
      try { var r = api.set_titlebar(t); if (r && r.catch) r.catch(function(){}); } catch (e) {}
    }
  }
  window.addEventListener('akthemechange', push);
  window.addEventListener('pywebviewready', push);
  push();
})();"""


def _colorref(hexstr):
    """#RRGGBB → COLORREF(0x00BBGGRR)：DWM 属性的字节序与 CSS 相反。"""
    n = int(hexstr.lstrip("#"), 16)
    return ((n & 0xFF) << 16) | (n & 0xFF00) | ((n >> 16) & 0xFF)


def _find_hwnds(needle="看板"):
    """按标题子串枚举本进程的顶层窗口（pywebview 的 WinForms 窗）。用子串而非
    全等：真实页的 <title> 会被 WebView2 同步成「xx · A股看板」，启动页才是
    「AK 看板」。不筛可见性——hidden=True 先藏后显的窗口也要能刷上色。"""
    if os.name != "nt":
        return []
    import ctypes
    u32 = ctypes.windll.user32
    pid = os.getpid()
    hits = []

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _lp):
        wpid = ctypes.c_uint()
        u32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(wpid))
        if wpid.value == pid:
            buf = ctypes.create_unicode_buffer(128)
            u32.GetWindowTextW(ctypes.c_void_p(h), buf, 128)
            if needle in buf.value:
                hits.append(h)
        return 1

    cb = proto(_cb)
    try:
        u32.EnumWindows(cb, None)
    except Exception:
        pass
    return hits


def _paint_titlebar(hwnds, theme):
    if os.name != "nt" or not hwnds:
        return
    import ctypes
    bg, fg, dark = _TITLEBAR_COLOR.get(theme) or _TITLEBAR_COLOR["splash"]
    want = 1 if dark else 0
    for h in hwnds:
        try:
            u32 = ctypes.windll.user32
            dwm = ctypes.windll.dwmapi
            if not u32.IsWindow(ctypes.c_void_p(h)):
                continue

            def _set(attr, val, _h=h):
                v = ctypes.c_uint(val)
                return dwm.DwmSetWindowAttribute(ctypes.c_void_p(_h),
                                                 ctypes.c_uint(attr),
                                                 ctypes.byref(v), ctypes.c_uint(4))

            # 深浅开关管最小化/关闭按钮区的底色（Win10 20H1+ 为 20，预览版曾为 19）；
            # Win11 深色主题必须开，否则深标题栏配浅按钮发灰
            if _set(20, want) != 0:
                _set(19, want)
            _set(35, _colorref(bg))   # DWMWA_CAPTION_COLOR（Win11 22000+）
            _set(36, _colorref(fg))   # DWMWA_TEXT_COLOR
        except Exception:
            pass   # 单窗失败不影响其余窗口


def _sync_titlebar(theme, ensure=False):
    """把 theme 对应的色刷到本程序全部顶层窗口。句柄没就绪（loaded 与窗口
    创建/显示的竞态）且 ensure=True 时后台轮询兜底；--web 降级无窗口则静默放弃。"""
    if os.name != "nt":
        return
    _titlebar_now[0] = theme
    hits = _find_hwnds()
    if hits:
        _titlebar_hwnds[:] = hits
        _paint_titlebar(hits, theme)
        _trace("titlebar %s painted on %d hwnd(s)" % (theme, len(hits)))
        return
    if not ensure:
        return

    def _retry():
        for _ in range(40):   # 10s：超时视为无窗口（浏览器回退），放弃
            time.sleep(0.25)
            if _titlebar_now[0] != theme:
                return   # 期间主题已更新，交给最新一次调用
            hits = _find_hwnds()
            if hits:
                _titlebar_hwnds[:] = hits
                _paint_titlebar(hits, theme)
                _trace("titlebar %s painted on %d hwnd(s) (retry)" % (theme, len(hits)))
                return

    threading.Thread(target=_retry, daemon=True, name="ak-titlebar").start()


class _ThemeApi:
    """暴露给页面的 js_api（window.pywebview.api）：页面把 data-theme 推过来，
    Python 用 DWM 把标题栏染成同主题色。仅此一个出入口，页面零侵入。"""

    def set_titlebar(self, theme):
        try:
            _trace("set_titlebar %r" % theme)
            _sync_titlebar(str(theme))
        except Exception:
            _trace("set_titlebar FAILED: %r" % (sys.exc_info()[1],))


# ---------- server 后台线程 ----------

def _server_worker(server, argv, stop, start_mod, port):
    """server.py 原样跑在守护线程；异常退出按 start.py 同款口径指数退避重启
    （存活 ≥15s 视为偶发退 3s，秒死翻倍到 60s 防空转）。窗口关闭走 taskkill，
    本循环只在窗口还开着期间兜住服务静默死亡。"""
    backoff = 3
    while True:
        started = time.monotonic()
        sys.argv = [server] + argv
        try:
            import runpy
            runpy.run_path(server, run_name="__main__")
        except SystemExit:
            pass
        except BaseException:
            import traceback
            traceback.print_exc()
            # 双开竞态兜底：两个实例几乎同时启动时，后绑定的会一直 bind 失败；
            # 端口若已被另一看板实例接管，就别空转重启了——窗口指向的端口
            # 本来就通（落在对方实例上），转为纯窗口模式即可。
            try:
                if start_mod.peer_is_our_service(port) is True:
                    print("[supervisor] 端口已由另一看板实例接管，本实例转为纯窗口模式。")
                    return
            except Exception:
                pass
        if stop.is_set():
            return
        uptime = time.monotonic() - started
        backoff = 3 if uptime >= 15 else min(backoff * 2, 60)
        print(f"[supervisor] {time.strftime('%F %T')} 服务线程退出"
              f"（存活 {int(uptime)}s），{backoff}s 后自动重启\n", end="")
        stop.wait(backoff)


def _wait_port(start_mod, port, timeout=30):
    t0 = time.monotonic()
    while not start_mod.port_in_use(port) and time.monotonic() - t0 < timeout:
        time.sleep(0.3)
    return start_mod.port_in_use(port)


# ---------- 入口 ----------

def run(root, argv, keep_console=False):
    """launcher 在「非 .py 转发、--web 未指定」时调用。成功则不返回
    （自起服务模式关窗即 taskkill 退出）；起不来抛 GUIUnavailable。"""
    smoke = os.environ.get("AK_GUI_SMOKE") == "1"
    if keep_console:
        _ensure_console()
    else:
        _hide_console()
    try:
        _run(root, argv, keep_console, smoke)
    except GUIUnavailable:
        _restore_console()
        raise


def _run(root, argv, keep_console, smoke):
    _trace("entry root=%r argv=%r smoke=%s" % (root, argv, smoke))
    server = os.path.join(root, "server.py")
    try:
        import webview   # noqa: F401  pywebview 未随包安装 → 回退浏览器
    except Exception as exc:
        _trace("import webview FAILED: %r" % exc)
        raise GUIUnavailable(f"pywebview 不可用（{exc}）")
    _trace("import webview ok")
    start_mod = _load_start(root)
    _trace("start module loaded")

    port = int(_argval(argv, "--port") or os.environ.get("AK_PORT") or 8000)
    own = True
    if start_mod.port_in_use(port):
        if start_mod.peer_is_our_service(port) is True:
            own = False   # 已有看板实例在跑：附身开第二个窗口，不再起服务
        else:
            for p in range(port + 1, port + 41):
                if not start_mod.port_in_use(p):
                    print(f"[看板] 端口 {port} 已被其他程序占用，改用 {p}。")
                    port = p
                    break
            else:
                raise GUIUnavailable(f"端口 {port} 被占且 {port + 1}-{port + 40} 无空位")
    url = f"http://127.0.0.1:{port}{start_mod.landing_path()}"
    _trace("own=%s port=%d url=%s" % (own, port, url))

    if own:
        # server 的 print 全部进 .status/logs/server.log（轮转同 start.py）；
        # 控制台被藏后这是唯一的日志出口，崩溃 traceback 可回查。
        try:
            logf = start_mod.rotate_server_log()
            _trace("rotate ok -> %r" % getattr(logf, "name", logf))
        except Exception as exc:
            logf = None
            _trace("rotate FAILED: %r" % exc)
        if logf is not None:
            if keep_console:
                sys.stdout = _Tee(sys.stdout, logf)
                sys.stderr = _Tee(sys.stderr, logf)
            else:
                sys.stdout = logf
                sys.stderr = logf
        stop = threading.Event()
        # GUI 模式必须压掉 server 的自动开浏览器（窗口就是浏览器替代品）
        server_argv = argv if "--no-open" in argv else argv + ["--no-open"]
        threading.Thread(target=_server_worker, args=(server, server_argv, stop,
                                                      start_mod, port),
                         daemon=True, name="ak-server").start()
        _trace("server worker started")

    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        # 轮动/复盘页的「导出小结」是 <a download> blob 下载，pywebview 默认禁下载会静默失效
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass   # 设置项改名/缺失只影响外链与导出去向，不阻塞窗口

    storage = os.path.join(root, STORAGE_REL)
    try:
        os.makedirs(storage, exist_ok=True)
    except OSError:
        storage = None   # 退回 pywebview 默认（localStorage 不持久，可接受）

    # 启动页直显：own=True 时窗口先加载内联启动页（毫秒级 loaded → show），
    # 服务由后台线程 _goto 等就绪后导航过去；own=False（附身模式）服务本就在跑，直达。
    win = webview.create_window(
        "AK 看板",
        _SPLASH_HTML if own else url,
        js_api=_ThemeApi(),   # 页面经此上报主题，标题栏跟随染色
        min_size=MIN_SIZE,
        hidden=True,   # 先藏后显：等首屏 loaded 再 show，避免白屏闪
        **_load_geo(root),
    )
    _trace("window created url=%s" % ("<splash>" if own else url))

    shown = {"done": False}

    def _on_loaded(*_a):
        if not shown["done"]:
            shown["done"] = True
            try:
                win.show()
            except Exception:
                pass
            _trace("window shown")
        # 每次导航落地都要重做（JS 上下文/句柄都可能换了）：先按当前主题刷
        # 标题栏（启动页深底，不闪系统黑），再注入桥让页面把真实主题推过来
        try:
            _sync_titlebar(_titlebar_now[0], ensure=True)
            win.evaluate_js(_BRIDGE_JS)
        except Exception:
            pass   # 染色失败只是观感问题

    def _on_closing(*_a):
        _save_geo(root, win)

    win.events.loaded += _on_loaded
    win.events.closing += _on_closing
    threading.Thread(target=lambda: (time.sleep(10), _on_loaded()),
                     daemon=True).start()   # loaded 迟迟不触发时兜底显示窗口

    if own:
        def _goto():
            if not _wait_port(start_mod, port, timeout=45):
                _trace("port NOT ready in 45s")
                print("[看板] 服务迟迟未就绪，仍尝试进入页面；若白屏，就绪后按 F5 即可。")
            win.events.loaded.wait(15)   # 启动页加载完、窗口已显示后再导航
            try:
                win.load_url(url)
                _trace("navigated -> %s" % url)
            except Exception:
                _trace("load_url FAILED: %r" % sys.exc_info()[1])
        threading.Thread(target=_goto, daemon=True, name="ak-goto").start()

    if smoke:
        def _smoke():
            win.events.loaded.wait(20)
            url_seen, t0 = "", time.monotonic()
            # 启动页由 pywebview 内置 HTTP 服务承载（也是 http:// 开头），
            # 必须等到导航进目标 url 才算就绪
            while time.monotonic() - t0 < 25:
                try:
                    u = win.get_current_url() or ""
                except Exception:
                    u = ""
                if u.startswith(url):
                    url_seen = u
                    break
                time.sleep(0.3)
            time.sleep(1)
            print(f"[gui-smoke] loaded={win.events.loaded.is_set()} page_url={url_seen}")
            win.destroy()
        threading.Thread(target=_smoke, daemon=True).start()

    try:
        webview.start(
            gui="edgechromium",   # 明确指定：宁可回退浏览器也不要掉进 ie 内核的 mshtml
            debug=os.environ.get("AK_WEBVIEW_DEBUG") == "1",
            private_mode=False,   # 主题/自选等 localStorage 要跨启动持久
            **({"storage_path": storage} if storage else {}),
        )
        _trace("webview.start returned normally")
    except Exception as exc:
        import traceback
        _trace("webview.start FAILED: %s" % traceback.format_exc())
        raise GUIUnavailable(f"WebView2 窗口启动失败（{exc}）")

    # 到这里 = 窗口已关。自起服务 → taskkill 进程树（抓取子进程一起退）；
    # 附身模式（服务是别人的）→ 只退本窗口进程，服务留在原进程里。
    if own:
        _trace("closing: taskkill tree")
        print(f"[看板] 窗口已关闭，退出（{time.strftime('%F %T')}）")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        if os.name == "nt":
            # taskkill 是控制台工具：窗口子系统父进程拉起会闪黑窗，压掉
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        os._exit(0)
