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
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse

from server_context import (MAIN_WEB, WRITE_TOKEN_ENV, READ_TOKEN_ENV,
                            _CACHE_LOCK, _PROC_LOCK, _PENDING, _fetch_procs,
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
        if self.close_connection:
            # 请求体无法安全消费时（见 _drain_body）提前判死连接：响应头告知客户端
            # 断连（send_header("Connection","close") 同时会置 close_connection），
            # 客户端不再在此连接上追加请求，残留 body 无从污染。
            self.send_header("Connection", "close")
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

    _MAX_DRAIN = 32 * 1024 * 1024   # 请求体预读上限：超限不读、直接判死连接

    def _drain_body(self):
        """keep-alive 纪律：do_POST 入口先把请求 body 读完缓冲（self._req_body）。

        HTTP/1.1 同连接串行复用：任何 POST 路径——写闸门 403/401、读闸门 401、
        抓取 409/200（fetch 系 handler 不读 body）、未知路由 404——只要响应前没把
        body 消费掉，残留字节就会被同一连接的下一个请求当成请求行（实测 501/400
        错乱）。统一在 do_POST 入口按 Content-Length 预读，_body() 改从缓冲取，
        handler 零改动。无法确定长度（chunked）、长度异常或超过 _MAX_DRAIN 时
        置 close_connection（_json 会随之带 Connection: close），宁可断连也不
        污染下一请求。
        """
        self._req_body = b""
        self._rejected = False   # 本请求已被拒绝并响应（413），do_POST 据此短路 handler
        raw = self.headers.get("Content-Length")
        if raw is None:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True   # chunked 等无法按长度预读
            return
        try:
            n = int(raw)
        except ValueError:
            self.close_connection = True
            return
        if n <= 0:
            return
        if n > self._MAX_DRAIN:
            # 超限不读（防内存膨胀，2026-09-30）：413 拒绝并判死连接，handler 不再执行
            self.close_connection = True
            self._json({"error": "payload too large"}, 413)
            self._rejected = True
            return
        try:
            data = self.rfile.read(n)
        except OSError:
            self.close_connection = True
            return
        if len(data) < n:   # 客户端中途断开：流已不可信
            self.close_connection = True
        self._req_body = data

    def _body(self):
        raw = getattr(self, "_req_body", None)
        if raw is None:   # 防御：未经 do_POST 预读的入口，按旧逻辑自读
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
            except Exception:
                return None
        try:
            return json.loads(raw.decode("utf-8") or "{}")
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
           AK_WRITE_TOKEN 时必须携带 X-AK-Token 头（或 ?token=）；绑定非回环
           地址而未显式配置时，server 启动会自动生成随机令牌并注入环境变量
           （见 server._ensure_lan_write_token），写面不再零鉴权；仅回环绑定
           且未配置时保持旧行为放行。本机来源始终放行。
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
                                 src_netloc, host, self._safe_path(self.path))
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
        # 常数时间比较（与读闸门同口径，2026-09-30）：普通 == 在局域网可按计时逐字节爆破。
        # 编码成 bytes 再比：compare_digest 对含非 ASCII 码点的 str 抛未捕获
        # TypeError（X-AK-Token 头经 latin-1 解码可含任意码点、?token= 经
        # parse_qs 可解出非 ASCII），401 会变成连接异常中断；bytes 比较无此
        # 限制，非 ASCII token 正常判不匹配落 401。
        if got and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8")):
            return True
        self._json({"error": "write token required"}, 401)
        self.log_message("write rejected: missing token %s", self._safe_path(self.path))
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
        got = (self.headers.get("X-AK-Token") or ""
               or self._query().get("token", [""])[0])
        if got and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8")):
            return True
        self._json({"error": "read token required"}, 401)
        self.log_message("read rejected: missing token %s", self._safe_path(self.path))
        return None

    # ---------------- 入站 Host 校验（2026-09-30，防 DNS rebinding · CWE-350） ----------------

    def _host_allowed(self):
        """Host 头必须指向本服务，否则拒绝。这是 CSRF 同源判定的前置防线：

        rebinding 攻击把攻击者域名 TTL=0 解析到 127.0.0.1，浏览器发来的
        Origin 与 Host 天然同值，写闸门的「Origin==Host 即同源」判定被整体
        绕过——所以先校验 Host 本身是否本服务合法名。合法名三类：
          - 回环（127.0.0.1 / localhost / ::1 / 0.0.0.0）
          - 本次连接的服务端地址（accepted socket 的 getsockname：手机同
            Wi-Fi 访问 LAN IP、Tailscale 访问 100.x 段均天然覆盖，DHCP
            换网也不用维护白名单）
          - 环境变量 AK_ALLOWED_HOSTS 追加（逗号分隔；内网穿透自有域名用）
        HTTP/1.0 客户端可不带 Host（rebinding 由浏览器发起必带），空头放行。
        """
        host = self.headers.get("Host") or ""
        if not host:
            return True
        hostname = urllib.parse.urlparse(f"//{host}").hostname
        if not hostname:
            return False
        hostname = hostname.lower()
        if hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return True
        extra = os.environ.get("AK_ALLOWED_HOSTS", "")
        if extra and hostname in {h.strip().lower() for h in extra.split(",") if h.strip()}:
            return True
        try:
            return hostname == (self.connection.getsockname()[0] or "").lower()
        except OSError:
            return False

    def _reject_bad_host(self):
        # 拒绝发生在 do_POST 的 _drain_body 之前：判死连接，防残留 body 污染
        # keep-alive 下一请求（与 _drain_body 的 keep-alive 纪律同一口径）。
        self.close_connection = True
        self._json({"error": "bad host"}, 403)
        self.log_message("rejected: bad host %r", self.headers.get("Host") or "")

    # ---------------- 日志脱敏（token 不落盘，2026-09-30） ----------------

    def _safe_path(self, raw):
        """路径+查询串，token 参数值替换为 ***：拒绝日志与访问日志都会整行写
        stderr（→ .status/logs/server.log），?token= 是 README 文档化用法，
        令牌不能明文落盘。"""
        parts = urllib.parse.urlparse(raw)
        if not parts.query:
            return raw
        q = [(k, "***" if k.lower() == "token" else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
        return urllib.parse.urlunparse(parts._replace(query=urllib.parse.urlencode(q, safe="*")))

    def _safe_requestline(self):
        parts = (self.requestline or "").split()
        if len(parts) >= 2:
            parts[1] = self._safe_path(parts[1])
            return " ".join(parts)
        return self.requestline or ""

    def log_request(self, code="-", size="-"):
        # BaseHTTPRequestHandler 默认把整行 requestline（含 ?token=<真值>）写日志；
        # 成功请求同样落 server.log，这里脱敏后复用 log_message。字段与位次须与
        # CPython 默认实现一致：requestline + 状态码 + size（此前漏传 code，访问
        # 日志的状态码恒为 "-"，200/304/401/403 无法区分）。
        self.log_message('"%s" %s %s', self._safe_requestline(), str(code), str(size))

    # ---------------- 后台抓取（各 fetch POST 与自愈共用） ----------------

    def _spawn_fetch(self, script, cwd, logfile, lock, key, label, extra=None, respond=True):
        """启动后台抓取：文件锁防跨进程重入，日志写 .status/logs/。返回 _json 已处理则 True。

        锁纪律：_PROC_LOCK 只护 _fetch_procs 的读写与「查+占位」的原子性，
        Popen/写日志在锁外做——写 socket（409 响应）不能发生在持锁时，
        否则一个慢客户端能拖住所有并发抓取请求。占位塞 _PENDING 哨兵
        （None 与「无记录」不可区分，会让同 key 并发请求双双 Popen），
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
            # 占位 _PENDING 与活进程同样算「已在跑」——若把占位排除在 running 之外，
            # 同 key 并发请求会双双通过检查各自 Popen（2026-10-06 修复）
            running = proc is _PENDING or (proc is not None and proc.poll() is None)
            if not running:
                _fetch_procs[key] = _PENDING   # 占位：同 key 并发请求在此等价于「已在跑」
        if running:
            # 409 写 socket 必须在锁外（上方锁纪律）：慢客户端阻塞发送时不能拖住
            # _PROC_LOCK，否则 health 轮询与全部抓取接口的进程簿记一并卡死。
            if respond:
                self._json({"status": "running"}, 409)
            return True
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
