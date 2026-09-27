# -*- coding: utf-8 -*-
"""Handler 全球总览域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：GET /api/global、POST /api/fetch-global。
"""
import os

from server_context import (GLOBAL_JSON, GLOBAL_FETCH_SCRIPT, GLOBAL_FETCH_CWD,
                            LOCK_GLOBAL, LOG_DIR, _cached_json_bytes)


class HandlerGlobal:
    def api_global(self):
        # 全球总览（~429KB，一天一变）：mtime 缓存的 bytes + ETag/304，
        # 轮动页隔夜外围条 10 分钟轮询与全球页刷新在数据未变时零开销
        cached = _cached_json_bytes(GLOBAL_JSON)
        if cached is None:
            return self._json({"error": "尚无全球总览数据，请先 POST /api/fetch-global 或运行 backend/global/fetch_global.py"}, 404)
        return self._send_etag_body(cached[0], cached[1], cached[2])

    def post_fetch_global(self):
        done = self._spawn_fetch(
            GLOBAL_FETCH_SCRIPT, GLOBAL_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-global.log"), LOCK_GLOBAL,
            "global", "global")
        return self._fetch_started() if not done else None
