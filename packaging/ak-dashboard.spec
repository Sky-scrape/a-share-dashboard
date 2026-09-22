# -*- mode: python ; coding: utf-8 -*-
"""AK 看板 PyInstaller 打包配置（onedir / onefile 双形态）。

默认 onedir：产物 dist/ak-dashboard/（exe + _internal/ 内嵌源码树），
    启动快、路径直观，适合计划任务/开机自启。
环境变量 AK_ONEFILE=1 切 onefile：产物 dist/ak-dashboard-onefile.exe，
    单文件零残留；运行时源码与数据常驻 %LOCALAPPDATA%\ak-dashboard
    （launcher 首启同步，见 launcher.py 头注释）。

构建（仓库根目录执行）：
    .venv-ci/Scripts/python.exe -m PyInstaller packaging/ak-dashboard.spec --noconfirm
"""
import os

from PyInstaller.utils.hooks import collect_all

ONEFILE = os.environ.get("AK_ONEFILE") == "1"
ROOT = os.path.dirname(SPECPATH)   # spec 在 packaging/ 下，ROOT 即仓库根

# 运行产物 / 缓存目录（相对仓库根），整目录剔除，绝不出现在交付包里
PRUNE = {
    os.path.join("backend", "recap", ".ht_cache"),
    os.path.join("backend", "quant", "data"),
    os.path.join("quant", "data"),
    os.path.join("quant", "results"),
    os.path.join("quant", "strategies_store"),
    os.path.join("quant", "notebooks"),
}


def add_tree(name):
    """整目录按原相对结构进包，剔除 __pycache__ / 字节码 / PRUNE 运行产物。"""
    out = []
    top = os.path.join(ROOT, name)
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(
            d for d in dirnames
            if d != "__pycache__"
            and os.path.relpath(os.path.join(dirpath, d), ROOT) not in PRUNE)
        rel = os.path.relpath(dirpath, ROOT)
        for f in sorted(filenames):
            if f.endswith((".pyc", ".pyo")):
                continue
            out.append((os.path.join(dirpath, f), rel))
    return out


datas = []
datas += add_tree("web")
datas += add_tree("backend")
datas += add_tree("quant")
datas += [(os.path.join(ROOT, "server.py"), "."),
          (os.path.join(ROOT, "start.py"), ".")]

# 第一方模块全部以源码形态随包分发并由 runpy 执行（launcher 转发），
# 因此这里只需要保证第三方包被收进 exe：fetch 子进程（runpy 源码）import
# requests/akshare 等时，由 exe 内置的 frozen importer 供给。
binaries = []
hiddenimports = ["requests", "numpy", "pandas", "pyarrow", "tzdata"]
for pkg in ("akshare", "py_mini_racer", "mini_racer", "tzdata"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue   # 未安装的包名（如 mini_racer 旧名）直接跳过
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT,
            os.path.join(ROOT, "backend"),
            os.path.join(ROOT, "backend", "recap"),
            os.path.join(ROOT, "backend", "quant"),
            os.path.join(ROOT, "backend", "auction")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="ak-dashboard-onefile",
        debug=False,
        strip=False,
        upx=False,
        console=True,
        exclude_binaries=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ak-dashboard",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="ak-dashboard",
    )
