# -*- coding: utf-8 -*-
"""
大盘可视化本地服务：标准库实现，无需安装 Flask。
提供数据 API + 静态文件服务。

用法:
    python server.py            # 默认 127.0.0.1:8000
    python server.py --port 8080 --host 0.0.0.0

API:
    GET /api/boards             板块清单
    GET /api/dates              已有数据的日期列表
    GET /api/day?date=YYYY-MM-DD 当日板块分时（缺省返回最新一天）
    GET /                        前端仪表盘
"""
import argparse
import http.server
import json
import os
import socketserver
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR  # noqa: E402

DAILY_DIR = os.path.join(DATA_DIR, "daily")
BOARDS_FILE = os.path.join(DATA_DIR, "boards.json")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def list_dates():
    if not os.path.isdir(DAILY_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(DAILY_DIR) if f.endswith(".json"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/dates":
            return self._json({"dates": list_dates()})
        if path == "/api/day":
            import re
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")
            dates = list_dates()
            if not dates:
                return self._json({"error": "暂无数据，请先运行 python fetch_day.py"}, 404)
            date = qs.get("date", [dates[-1]])[0]
            # 白名单校验：仅接受 YYYY-MM-DD，防路径穿越
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return self._json({"error": "日期格式非法"}, 400)
            data = self._read_json(os.path.join(DAILY_DIR, f"{date}.json"))
            if data is None:
                return self._json({"error": f"没有 {date} 的数据"}, 404)
            return self._json(data)
        if path == "/api/boards":
            boards = self._read_json(BOARDS_FILE)
            if boards is None:
                return self._json({"error": "暂无板块清单"}, 404)
            return self._json(boards)
        return super().do_GET()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    dates = list_dates()
    print("=" * 50)
    print("A股板块日内轮动可视化")
    print(f"  已采集数据: {', '.join(dates) if dates else '（无，先运行 python fetch_day.py）'}")
    print(f"  访问: http://{args.host}:{args.port}")
    print("  Ctrl+C 退出")
    print("=" * 50)
    with ThreadingServer((args.host, args.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")


if __name__ == "__main__":
    main()
