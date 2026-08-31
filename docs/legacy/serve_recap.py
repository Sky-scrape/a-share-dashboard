# -*- coding: utf-8 -*-
"""本地看板服务。

用法：
    python backend/serve.py            # 默认端口 8000，自动打开浏览器
    python backend/serve.py --port 8080

路由：
    /             → web/index.html
    /data/YYYYMMDD.json → 快照文件（白名单：仅 8 位数字日期）
    /api/dates    → {"dates": ["20260805", ...]}（按日期倒序）
    POST /api/fetch → 后台触发一次抓取（防重入，进行中返回 409）

安全边界：静态服务只暴露 web/ 目录；data/ 仅放行合法快照名，杜绝目录浏览与路径穿越。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")
FETCH_SCRIPT = os.path.join(ROOT, "backend", "fetch_daily.py")

_fetch_proc = None  # 正在运行的手动抓取进程（防重入）


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self):
        # 看板数据实时性重要：禁止浏览器启发式缓存
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/dates":
            dates = sorted(
                (f[:-5] for f in os.listdir(DATA_DIR) if f.endswith(".json")),
                reverse=True,
            )
            return self._json({"dates": dates})
        if path.startswith("/data/"):
            # 白名单：data/<8位数字>.json，防目录穿越
            name = path[len("/data/"):]
            if not (len(name) == 13 and name.endswith(".json") and name[:8].isdigit()):
                return self._json({"error": "not found"}, 404)
            fp = os.path.join(DATA_DIR, name)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            try:
                with open(fp, encoding="utf-8") as f:
                    return self._json(json.load(f))
            except Exception:
                return self._json({"error": "snapshot broken"}, 500)
        # 其余静态文件仅从 web/ 提供（构造时已限定 directory）
        return super().do_GET()

    def do_POST(self):
        global _fetch_proc
        path = urlparse(self.path).path
        if path == "/api/fetch":
            if _fetch_proc is not None and _fetch_proc.poll() is None:
                return self._json({"status": "running"}, 409)
            try:
                log = open(os.path.join(DATA_DIR, "fetch.log"), "a", encoding="utf-8")
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] ==== manual fetch start ====\n")
                log.flush()
                _fetch_proc = subprocess.Popen(
                    [sys.executable, FETCH_SCRIPT],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                return self._json({"status": "error", "error": str(e)}, 500)
            return self._json({"status": "started"})
        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # 更安静
        sys.stderr.write(f"[serve] {self.address_string()} {fmt % args}\n")


def main():
    ap = argparse.ArgumentParser(description="A股复盘看板本地服务")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"看板已启动: {url}")
    print("按 Ctrl+C 停止")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
