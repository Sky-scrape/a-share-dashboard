# -*- coding: utf-8 -*-
"""hithink-finance CLI 定位（单一来源，2026-09-28）。

PATH 优先，找不到再兜底显式查 npm 全局目录（%APPDATA%\\npm）：开机自启/
计划任务拉起的进程只带注册表 PATH，用户 PATH 一旦缺了 %APPDATA%\\npm
（实测：该目录曾被整段粘贴的 PATH 覆盖而丢失），which 落空但文件其实在——
抓取层不该因此断源。消费方：start.py 预检、backend/recap/ht.py。
"""
import os
import shutil

_NAMES = ("hithink-finance.cmd", "hithink-finance")   # Windows npm 全局是 .cmd


def find_exe():
    """返回 hithink-finance 可执行文件路径；找不到返回 None。"""
    exe = shutil.which("hithink-finance") or shutil.which("hithink-finance.cmd")
    if exe:
        return exe
    appdata = os.environ.get("APPDATA")
    if appdata:
        for name in _NAMES:
            cand = os.path.join(appdata, "npm", name)
            if os.path.isfile(cand):
                return cand
    return None
