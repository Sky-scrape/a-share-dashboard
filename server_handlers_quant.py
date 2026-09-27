# -*- coding: utf-8 -*-
"""Handler 量化平台域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：GET /api/quant、/api/quant/meta、/api/quant/strategies、
/api/quant/jobs、/api/quant/job/<id>；POST /api/quant/backtest、
/api/quant/strategies[|/get|/delete]、/api/quant/job、/api/quant/compare、
/api/quant/rule/preview。
"""
from server_context import quant_api, quant_payload


class HandlerQuant:
    # ---------------- GET handlers：量化 ----------------

    def api_quant(self):
        force = self._query().get("fresh", [""])[0] in ("1", "true")
        return self._json(quant_payload(force=force))

    def api_quant_meta(self):
        try:
            return self._json(quant_api.meta(force=self._query().get("fresh", [""])[0] in ("1", "true")))
        except Exception as e:
            return self._json({"error": f"meta 失败：{e}"}, 500)

    def api_quant_strategies(self):
        try:
            return self._json({"strategies": quant_api.strategies_list()})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def api_quant_jobs(self):
        try:
            return self._json(quant_api.jobs_list())
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def api_quant_job_get(self, jid):
        try:
            return self._json(quant_api.job_get(jid))
        except Exception as e:
            return self._json({"error": str(e)}, 400)

    # ---------------- POST handlers：量化 ----------------

    def post_quant_backtest(self):
        req = self._body()
        if req is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        try:
            return self._json(quant_api.run_backtest_api(req))
        except Exception as e:
            return self._json({"error": str(e)[:600]}, 400)

    def post_quant_strategies_save(self):
        req = self._body()
        if req is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        try:
            return self._json(quant_api.strategies_save(req))
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_strategies_get(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.strategies_get(req.get("name", "")))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 404)

    def post_quant_strategies_delete(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.strategies_delete(req.get("name", "")))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)

    def post_quant_job(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.job_start(req.get("kind", ""), req.get("params") or {}))
        except quant_api.BlockingError as e:
            return self._json({"error": str(e)}, 409)
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_compare(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.compare_api(req))
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_rule_preview(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.rule_preview(req.get("spec") or req))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)
