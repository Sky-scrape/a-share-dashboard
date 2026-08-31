# -*- coding: utf-8 -*-
"""pytest 单元测试的导入引导：把项目根与 backend 各目录放进 sys.path，
使 tests/ 下的纯函数单测能直接 import backend 内的扁平模块。
（quant/tests 有自己的引导；这里只服务 backend 侧。）"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "backend"), os.path.join(ROOT, "backend", "recap"),
          os.path.join(ROOT, "backend", "quant"), os.path.join(ROOT, "backend", "auction")):
    if p not in sys.path:
        sys.path.insert(0, p)
