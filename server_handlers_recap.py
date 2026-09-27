# -*- coding: utf-8 -*-
"""Handler 复盘域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：/api/sentiment、/api/recap/dates、/api/recap/modules、
GET/POST /api/recap/note、POST /api/fetch。
"""
import os
import time

from server_context import (MODULE_REGISTRY, RECAP_NOTES, FETCH_SCRIPT,
                            FETCH_CWD, LOCK_RECAP, LOG_DIR, _DATE8_RE,
                            recap_dates, sentiment_payload, fsutil)


class HandlerRecap:
    # ---------------- GET handlers：复盘 ----------------

    def api_sentiment(self):
        return self._json(sentiment_payload())

    def api_recap_dates(self):
        return self._json({"dates": recap_dates()})

    def api_recap_modules(self):
        return self._json({"modules": [
            {"key": k, "title": t, "allow_empty": e} for k, t, e in MODULE_REGISTRY]})

    def api_recap_note(self):
        date = self._query().get("date", [""])[0]
        if not _DATE8_RE.fullmatch(date):
            return self._json({"error": "日期格式非法"}, 400)
        fp = os.path.join(RECAP_NOTES, date + ".md")
        content = ""
        saved_at = None
        try:
            with open(fp, encoding="utf-8") as f:
                content = f.read()
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(os.path.getmtime(fp)))
        except OSError:
            pass
        return self._json({"date": date, "content": content, "saved_at": saved_at})

    # ---------------- POST handlers：复盘抓取 / 复盘笔记 ----------------

    def post_fetch_recap(self):
        done = self._spawn_fetch(
            FETCH_SCRIPT, FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-recap.log"), LOCK_RECAP,
            "recap", "recap")
        return self._fetch_started() if not done else None

    def post_recap_note(self):
        date = self._query().get("date", [""])[0]
        if not _DATE8_RE.fullmatch(date):
            return self._json({"error": "日期格式非法"}, 400)
        body = self._body()
        if body is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        content = body.get("content")
        if not isinstance(content, str):
            return self._json({"error": "content 需为字符串"}, 400)
        if len(content) > 200000:
            return self._json({"error": "笔记过长（>200KB）"}, 400)
        os.makedirs(RECAP_NOTES, exist_ok=True)
        fp = os.path.join(RECAP_NOTES, date + ".md")
        try:
            # fsutil 原子写（临时文件 + os.replace + Windows 短重试，2026-09-11 收编：
            # 此处曾有一份同构内联实现，再往前 tmp 变量是死代码、直写目标文件）。
            # newline="\n"：笔记按 LF 落盘
            fsutil.save_text_atomic(fp, content, newline="\n")
        except Exception as e:
            return self._json({"status": "error", "error": str(e)}, 500)
        return self._json({"status": "saved",
                           "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
