# -*- coding: utf-8 -*-
"""Handler 页面与静态资源域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：五个页面（/recap、/global、/auction、/quant 与复盘页 app.js）、
/lib/<path> 共享静态（ETag/长缓存）、/quant-results/<file> 量化报告直链、
/data/YYYYMMDD.json 复盘快照。
"""
import os
import re

from server_context import (RECAP_WEB, GLOBAL_WEB, AUCTION_WEB, QUANT_WEB,
                            LIB_WEB, _LIB_CTYPES, _LIB_IMMUTABLE,
                            _snapshot_payload, quant_collector)


class HandlerWww:
    # ---------------- GET handlers：静态 / 快照 ----------------

    def api_lib_static(self, rest):
        """web/lib/ 共享资源：ETag 协商缓存；echarts/world.json 长缓存（immutable）。"""
        full = os.path.realpath(os.path.join(LIB_WEB, rest))
        if not full.startswith(os.path.realpath(LIB_WEB) + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = _LIB_CTYPES.get(ext)
        if not ctype:
            return self._json({"error": "not found"}, 404)
        url_path = "/lib/" + rest.replace("\\", "/")
        cache = "long" if url_path in _LIB_IMMUTABLE else "etag"
        return self._file(full, ctype, cache=cache)

    def api_quant_results(self, sub):
        # 量化自包含报告直链：/quant-results/<file> 与 /quant-results/research/<file>
        from urllib.parse import unquote
        sub = unquote(sub)
        if sub.startswith("research/"):
            root_dir, fname = os.path.join(quant_collector.QUANT_ROOT, "results", "research"), sub[len("research/"):]
        else:
            root_dir, fname = os.path.join(quant_collector.QUANT_ROOT, "results"), sub
        if not re.fullmatch(r"[\w.\-一-龥]+\.(html|json|csv)", fname or ""):
            return self._json({"error": "非法文件名"}, 400)
        fp = os.path.join(root_dir, fname)
        if not os.path.isfile(fp) or os.path.dirname(os.path.realpath(fp)) != os.path.realpath(root_dir):
            return self._json({"error": "not found"}, 404)
        ctype = {"html": "text/html; charset=utf-8",
                 "json": "application/json; charset=utf-8",
                 "csv": "text/csv; charset=utf-8"}[fname.rsplit(".", 1)[-1]]
        return self._file(fp, ctype)

    def api_snapshot(self, name):
        # /data/YYYYMMDD.json：白名单已由路由正则保证（仅 8 位数字.json），防路径穿越。
        # 快照按日期不可变（~420KB）：mtime 缓存解析 + ETag/304，复盘页切日期回看不重解压
        data = _snapshot_payload(name[:-5])
        if data is None:
            return self._json({"error": "snapshot missing or broken"}, 500)
        return self._etag_json(data)

    # ---------------- GET handlers：页面 ----------------

    def page_recap(self):
        return self._file(os.path.join(RECAP_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_recap_app_js(self):
        """复盘页脚本（2026-09-27 方案 O-3c 从 index.html 内联抽出，仍零构建）；
        ETag 协商缓存与 /lib 一致。"""
        return self._file(os.path.join(RECAP_WEB, "app.js"),
                          "text/javascript; charset=utf-8", cache="etag")

    def page_global(self):
        return self._file(os.path.join(GLOBAL_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_auction(self):
        return self._file(os.path.join(AUCTION_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_quant(self):
        return self._file(os.path.join(QUANT_WEB, "index.html"),
                          "text/html; charset=utf-8")
