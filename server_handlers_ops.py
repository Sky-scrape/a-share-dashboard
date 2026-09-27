# -*- coding: utf-8 -*-
"""Handler 运维域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：GET /api/health 数据管家（新鲜度/失败明细/锁状态；聚合在
server_context.health_payload，此处只做接线）。
"""
from server_context import health_payload


class HandlerOps:
    def api_health(self):
        return self._json(health_payload())
