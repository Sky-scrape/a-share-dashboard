# -*- coding: utf-8 -*-
"""Handler 公共底座 mixin（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。这里收各域共用的 HTTP 管线——
发送助手（_json/_etag_json/_send_etag_body/_file/_maybe_gzip/_cc）、请求解析
（_body/_query/_read_json）、读/写接口闸门（CSRF + 可选 Token）、后台抓取
进程簿记（_spawn_fetch/_fetch_started）与 keep-alive/缓存标记/end_headers/
log_message 纪律。方法体自 server.py 原样搬移（零行为变化）；共享状态一律
from server_context import ...，绝不 import server（防循环）。
"""
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse

from server_context import (MAIN_WEB, WRITE_TOKEN_ENV, READ_TOKEN_ENV,
                            _CACHE_LOCK, _PROC_LOCK, _fetch_procs,
                            _read_json_file, lockutil)


class HandlerCore:
    # HTTP/1.1 keep-alive：手机经 Tailscale 访问省去每请求 TCP 握手。
    # 纪律：所有 send 路径必须带 Content-Length（_json/_etag_json/_etag_body/_file 均已带；
    # 304 无体合法；SimpleHTTPRequestHandler 托管的静态文件自带 Content-Length；
    # send_error 亦自带）。空闲连接由类属性 timeout 兜底掐断，不占死线程。
    protocol_version = "HTTP/1.1"
    timeout = 60   # 空闲/僵死连接 60s 后断开（StreamRequestHandler.setup 里落到 socket）

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=MAIN_WEB, **kwargs)

    # ---------------- 发送助手 ----------------

    def _cc(self, value):
        """发 Cache-Control 并标记「已自行管理缓存」，end_headers 不再补 no-cache。"""
        self._ak_cc = True
        self.send_header("Cache-Control", value)

    def send_response(self, *a, **kw):
        # 每个响应开始时重置缓存标记：上一响应若在 _cc() 之后、end_headers 之前抛异常，
        # keep-alive 连接的下一个请求不能继承「已自管缓存」的状态（会漏补 no-cache）。
        self._ak_cc = False
        super().send_response(*a, **kw)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        body, enc = self._maybe_gzip(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cc("no-store")
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_gzip(self, body):
        """客户端接受 gzip 且体足够大时压缩（手机/Tailscale 拉 420KB 全球数据可降 ~5-8x）。
        返回 (body, encoding)；不压缩时 encoding=None。"""
        if len(body) < 1024:
            return body, None
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in ae:
            return body, None
        return gzip.compress(body, 6), "gzip"

    def _etag_json(self, obj):
        """带 ETag/304 的 JSON：轮询方数据未变时不回传整包（/api/day 同款）。"""
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"%s"' % hashlib.sha256(body).hexdigest()
        self._send_etag_body(body, etag)

    def _send_etag_body(self, body, etag, gz_slot=None):
        """按已算好的 ETag 发送预序列化 JSON bytes（304 或 200+gzip）。

        gz_slot：可选 [bytes 或 None] 单槽列表，gzip 结果跨请求复用
        （/api/day 等大 JSON 命中缓存后连压缩都省掉）。ETag 与压缩无关：
        同一数据无论客户端是否接受 gzip 都是同一 ETag，304 协商不受影响。

        复用槽位时仍须按本次请求的 Accept-Encoding 判定：槽位一旦被浏览器填满，
        若不复判就会把 gzip 发给没要压缩的客户端（curl/脚本拿到 1f 8b 二进制）。
        """
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        wants_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        gz = None
        if gz_slot is not None and wants_gzip:
            with _CACHE_LOCK:
                gz = gz_slot[0]
        if gz is not None:
            out, enc = gz, "gzip"
        else:
            out, enc = self._maybe_gzip(body)
            if enc and gz_slot is not None:
                with _CACHE_LOCK:
                    gz_slot[0] = out
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", etag)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _file(self, path, ctype, cache="etag"):
        """静态文件。cache=etag（页面与常改文件，304 协商缓存；页面从 no-store 改为
        协商后，刷新仍即时可见——文件一变 ETag 就变——但未变化的整页传输省掉）
        / long（vendored 大文件，max-age 长缓存 + 304 兜底）/ no-store（永不缓存）。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
            st = os.stat(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        etag = '"%s"' % hashlib.sha256(f"{st.st_size}-{st.st_mtime_ns}".encode()).hexdigest()
        if cache in ("etag", "long") and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self._cc("no-cache" if cache == "etag" else "public, max-age=86400, immutable")
            self.end_headers()
            return
        # 文本类按 Accept-Encoding 压缩（手机/Tailscale 拉 ~100KB 页面/1MB echarts 降数倍）；
        # ETag 由 mtime+size 决定、与压缩无关，协商语义不变（Accept-Encoding 判别照 _maybe_gzip）
        enc = None
        if ctype.startswith(("text/", "application/json")):
            body, enc = self._maybe_gzip(body)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        if cache == "long":
            self._cc("public, max-age=86400, immutable")
        elif cache == "etag":
            self._cc("no-cache")
        else:
            self._cc("no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, path):
        return _read_json_file(path)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return None

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # ---------------- 写接口闸门（2026-09-04 加固） ----------------

    def _client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _write_gate_ok(self):
        """POST 写接口闸门。返回 True 放行；返回 None 表示已拒绝（403/401 已发送）。

        1) CSRF：浏览器跨站 POST 必带 Origin（部分场景只带 Referer），与本服务
           Host 不同源 → 403。看板页面同源访问、手机经 Tailscale 同源访问不受影响。
        2) 局域网/外网直连（curl 等无浏览器头的客户端）：非本机来源在配置了
           AK_WRITE_TOKEN 时必须携带 X-AK-Token 头（或 ?token=）；未配置时保持
           旧行为放行（启动横幅提示如何开启）。本机来源始终放行。
        """
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        if origin or referer:
            src = origin or referer
            src_netloc = urllib.parse.urlparse(src).netloc
            if host and src_netloc.lower() != host.lower():
                self._json({"error": "cross-origin write rejected"}, 403)
                self.log_message("write rejected: cross-origin %s -> %s %s",
                                 src_netloc, host, self.path)
                return None
            return True   # 同源浏览器请求，放行
        # 无 Origin/Referer：非浏览器客户端
        if self._client_is_local():
            return True
        token = os.environ.get(WRITE_TOKEN_ENV, "").strip()
        if not token:
            return True   # 未配置 token：保持旧行为（启动横幅已提示）
        got = (self.headers.get("X-AK-Token") or ""
               or self._query().get("token", [""])[0])
        if got == token:
            return True
        self._json({"error": "write token required"}, 401)
        self.log_message("write rejected: missing token %s", self.path)
        return None

    # ---------------- 读接口闸门（2026-09-15，远程暴露场景可选） ----------------

    def _read_gate_ok(self):
        """数据面读闸门：仅当配置了 AK_READ_TOKEN 且客户端非本机时生效。

        覆盖 /api/*、/data/*、/quant-results/*（看板全部数据出口）；页面静态
        资源不拦（无数据即无泄露）。本机回环始终放行。返回 True 放行；
        返回 None 表示已拒绝（401 已发送）。"""
        if self._client_is_local():
            return True
        token = os.environ.get(READ_TOKEN_ENV, "").strip()
        if not token:
            return True
        import hmac as _hmac
        got = (self.headers.get("X-AK-Token") or ""
               or self._query().get("token", [""])[0])
        if got and _hmac.compare_digest(got, token):
            return True
        self._json({"error": "read token required"}, 401)
        self.log_message("read rejected: missing token %s", self.path)
        return None

    # ---------------- 后台抓取（各 fetch POST 与自愈共用） ----------------

    def _spawn_fetch(self, script, cwd, logfile, lock, key, label, extra=None, respond=True):
        """启动后台抓取：文件锁防跨进程重入，日志写 .status/logs/。返回 _json 已处理则 True。

        锁纪律：_PROC_LOCK 只护 _fetch_procs 的读写与「查+占位」的原子性，
        Popen/写日志在锁外做——写 socket（409 响应）不能发生在持锁时，
        否则一个慢客户端能拖住所有并发抓取请求。占位先塞 None，
        Popen 成功后替换，异常时清位。
        respond=False 供内部自愈调用（sector 自动补抓）：同一套锁与进程簿记，
        但不占用请求响应——「已在跑/失败」静默返回，留给下轮轮询观察结果。"""
        held = lockutil.held_info(lock)
        if held:
            if respond:
                self._json({"status": "running", "held": held}, 409)
            return True
        with _PROC_LOCK:
            proc = _fetch_procs.get(key)
            if proc is not None and proc.poll() is None:
                if respond:
                    self._json({"status": "running"}, 409)
                return True
            _fetch_procs[key] = None   # 占位：同 key 并发请求在此等价于「已在跑」
        try:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
            log = open(logfile, "a", encoding="utf-8")
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] ==== {label} manual fetch start ====\n")
            log.flush()
            p = subprocess.Popen(
                [sys.executable, script, *(extra or [])],
                cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with _PROC_LOCK:
                _fetch_procs[key] = p
        except Exception as e:
            with _PROC_LOCK:
                _fetch_procs.pop(key, None)
            if respond:
                self._json({"status": "error", "error": str(e)}, 500)
            return True
        return False

    def _fetch_started(self):
        return self._json({"status": "started"})

    # ---------------- 其它 ----------------

    def end_headers(self):
        # 我们自己的发送器都走 _cc() 自管缓存；只有 SimpleHTTPRequestHandler 托管的
        # 其余静态文件（favicon 等）在这里补 no-cache，保证样式/页面更新后刷新即见。
        if not getattr(self, "_ak_cc", False):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):  # 更安静
        sys.stderr.write(f"[server] {self.address_string()} {fmt % args}\n")
