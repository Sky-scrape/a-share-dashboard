# -*- coding: utf-8 -*-
"""本地门禁一键入口（2026-11-22 文档收尾；同日补全 ruff/node）。

一条命令跑完改完必跑的检查，顺序固定：

    1. pytest quant/tests tests -q   # 引擎 + 看板回归
    2. tests/smoke.py                # API 契约 + 前端结构（会临时起服务并写 .status/）
    3. tools/check_outbound.py       # backend 裸 requests / urllib 出站守卫
    4. tools/check_task_entries.py   # bat/vbs 行尾、BOM、编码 + 引用脚本存在且已跟踪
    5. ruff check .                  # Python 静态检查
    6. node --check web JS           # 前端语法（排除 vendored echarts）

任一失败返回 1，全过返回 0。解释器优先仓库内 .venv-ci
（Windows: .venv-ci/Scripts/python.exe；POSIX: .venv-ci/bin/python），
没有则回退当前解释器 sys.executable——实际用哪个会打印在第一行；用 .venv-ci
时会清掉裸 python 可能带入的 PYTHONHOME/PYTHONPATH（事故-H-06）。
ruff/node 必须可用；缺失即失败，不再静默跳过（2026-11-22 审查 low #2）。

用法：
    python tools/check.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pin_interpreter():
    """定位仓库 .venv-ci 解释器（没有则用 sys.executable），返回 (实际路径, 来源)。"""
    if os.name == "nt":
        venv_dir = ROOT / ".venv-ci" / "Scripts"
        names = ("python.exe", "python")
    else:
        venv_dir = ROOT / ".venv-ci" / "bin"
        names = ("python", "python3")
    if venv_dir.is_dir():
        for name in names:
            found = venv_dir / name
            if found.is_file():
                return str(found), "仓库 .venv-ci"
    return sys.executable, "回退 sys.executable"


def find_tool(name):
    """优先仓库 .venv-ci 的同名可执行文件，其次 PATH。"""
    if os.name == "nt":
        candidates = (ROOT / ".venv-ci" / "Scripts" / (name + ".exe"),
                      ROOT / ".venv-ci" / "Scripts" / name)
    else:
        candidates = (ROOT / ".venv-ci" / "bin" / name,)
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # 子进程输出前先落头部，避免管道下乱序
    actual, source = pin_interpreter()
    print("[check] 解释器：%s（%s）" % (actual, source))
    os.chdir(str(ROOT))

    # 裸 python 可能解到 LibreOffice 解释器并带 PYTHONHOME/PYTHONPATH（事故-H-06）；
    # 用 .venv-ci 时必须清掉，否则子进程的 stdlib 会被指到别的 Python。
    env = dict(os.environ)
    if source == "仓库 .venv-ci":
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)

    failed = []

    def record(label, code):
        if code != 0:
            failed.append((label, code))
            print("[check] %s FAILED（退出码 %s）" % (label, code))

    print("\n[check] >>> pytest quant/tests tests -q")
    res = subprocess.run(["python", "-m", "pytest", "quant/tests", "tests", "-q"],
                         executable=actual, env=env, cwd=str(ROOT), shell=False)
    record("pytest", res.returncode)

    print("\n[check] >>> tests/smoke.py")
    res = subprocess.run(["python", "tests/smoke.py"],
                         executable=actual, env=env, cwd=str(ROOT), shell=False)
    record("smoke", res.returncode)

    print("\n[check] >>> tools/check_outbound.py")
    res = subprocess.run(["python", "tools/check_outbound.py"],
                         executable=actual, env=env, cwd=str(ROOT), shell=False)
    record("outbound", res.returncode)

    task_checker = ROOT / "tools" / "check_task_entries.py"
    if task_checker.is_file():
        print("\n[check] >>> tools/check_task_entries.py")
        res = subprocess.run(["python", "tools/check_task_entries.py"],
                             executable=actual, env=env, cwd=str(ROOT), shell=False)
        record("task-entries", res.returncode)
    else:
        failed.append(("task-entries", "missing"))
        print("\n[check] >>> tools/check_task_entries.py")
        print("[check] task-entries FAILED：tools/check_task_entries.py 不存在")

    ruff = find_tool("ruff")
    if ruff:
        print("\n[check] >>> ruff check .")
        res = subprocess.run(["ruff", "check", "."],
                             executable=ruff, env=env, cwd=str(ROOT), shell=False)
        record("ruff", res.returncode)
    else:
        failed.append(("ruff", "missing"))
        print("\n[check] ruff FAILED：找不到 ruff（.venv-ci 或 PATH）")

    node = find_tool("node")
    if node:
        js = []
        js.extend(str(p) for p in ROOT.glob("web/*.js"))
        js.extend(str(p) for p in ROOT.glob("web/*/app.js"))
        js.extend(str(p) for p in ROOT.glob("web/lib/*.js"))
        js = [p for p in js if not p.endswith("echarts.min.js")]
        for p in js:
            label = "node --check %s" % os.path.relpath(p, ROOT)
            print("\n[check] >>> %s" % label)
            res = subprocess.run(["node", "--check", p],
                                 executable=node, env=env, cwd=str(ROOT), shell=False)
            record(label, res.returncode)
    else:
        failed.append(("node", "missing"))
        print("\n[check] node FAILED：找不到 node")

    print()
    if failed:
        print("[check] FAIL：" + "，".join("%s(rc=%s)" % (n, rc) for n, rc in failed))
        return 1
    print("[check] ALL PASS：pytest + smoke + outbound + task-entries + ruff + node")
    return 0


if __name__ == "__main__":
    sys.exit(main())
