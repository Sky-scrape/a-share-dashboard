# -*- coding: utf-8 -*-
"""providers 直跑自检入口（2026-09-27 拆包时自 providers.py 尾部迁入）。

包的 __init__.py 无法再被 `python providers.py` 当脚本直跑，自检块改由
`python -m providers` 触发（需 backend/recap 在 sys.path，如在 recap 目录下
执行）。仅人工探测用；日常抓取走 fetch_daily。
"""
import json
import time

import providers

providers.DATE = time.strftime("%Y%m%d")
for name, result in providers.fetch_all().items():
    print(json.dumps({name: result}, ensure_ascii=False)[:400])
