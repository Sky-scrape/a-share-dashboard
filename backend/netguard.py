# -*- coding: utf-8 -*-
"""出站 HTTP 的 SSRF 防线（单一来源）。

此前守卫在 us_market._safe_get 与 recap/spec_ohlc_fb._safe_get 各存一份，后者注释
自陈「us_market._safe_get 同款」。同一事实两处实现必然漂移——本项目已因此吃过一次
亏：backtest_capital 于 2026-09-10 修正 LU 出场止损口径（low_min 误当 risk_low），
spec_validate 直到 2026-09-24 才同步。按 execution_layer 抽 MF_BUY 的同一纪律，
把安全边界也收拢到一处。

防线两层：
  1. 协议 https + 主机白名单（调用方传字面量集合，不从外部输入拼协议/域名）
     —— 这是实际控制。
  2. DNS 解析结果落在受限地址段则阻断 —— 防 DNS rebinding 的纵深防御。

2026-09-24 修正（第 2 层误杀）：
  本地代理（Clash/mihomo 系，HTTP(S)_PROXY=127.0.0.1:7890）启用 fake-IP DNS 时，
  **所有**外部域名都解析进 198.18.0.0/15，而 Python 3.13+ 的 ipaddress 把该段计入
  is_private → 第 2 层把全部白名单主机一并拒绝。后果：us_close_task 的 57 个美股
  标的全部抓取失败、factors.json 缺当日行 → 选池 pending/empty；腾讯 OHLC 备源
  同挂 → 本地研究库缺当日全市场日线、验票无法完成。数据韧性实际退化为单源
  （hithink CLI 无此守卫），且失效表现为上游 57 条同质报错，不易归因。

  放行该段的依据：
    - 198.18.0.0/15 是 RFC 2544 基准测试保留段，公网不可路由。无代理时请求只会被
      网关丢弃，不会转到任何真实内网主机，因此不构成 SSRF 通道。
    - 该段由本机回环代理持有，代理按 **hostname** 而非此 IP 决定上游路由。
    - 第 1 层主机白名单是字面量硬编码，仍为实际控制；本豁免只放宽第 2 层。
  豁免严格限定为该 /15，不放行任何真实私网/环回/链路本地/组播段。

2026-10-27 起（上游失败现场采样）：
  本模块另承担「失效归因」的另一半：safe_get 失败分支自动把状态码/异常类型 +
  响应体头落 .status/upstream/（capture_sample，us_market / spec_ohlc_fb 的抓取
  都经此处）。动机与 diagnose() 同源——失效曾只表现为上游几十条同质报错
  （0924 fake-IP 误杀即如此），难辨限流页/改版页/本机断网。采样是诊断副线：
  任何失败都不外抛、不影响主链路；200 的正常响应永不采样（正常轮询零额外 I/O）。
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

_UA = {"User-Agent": "Mozilla/5.0"}

# 本地代理 fake-IP DNS 占用段（RFC 2544 基准测试保留，公网不可路由）。
# 见模块头「2026-09-24 修正」——新增网段前必须先确认它同样不可路由到真实内网。
PROXY_FAKEIP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def is_proxy_fakeip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """是否落在本地代理 fake-IP DNS 占用段（豁免判定单一来源）。

    出站守卫不止 http GET 一处（notify 推送的 is_global 检查同受 fake-IP 误杀，
    2026-09-25 审查 F8）——豁免与否的裁决收在这里，调用方只组合各自的语义。
    """
    return any(ip in net for net in PROXY_FAKEIP_NETS)


def is_restricted_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """受限地址判定：私网/环回/链路本地/保留/组播，但豁免本地代理 fake-IP 段。"""
    if is_proxy_fakeip(ip):
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast)


def check_url(url: str, allowed_hosts, port: int = 443) -> None:
    """校验协议/主机白名单/端口，并阻断解析到受限地址的目标。不通过抛 ValueError。

    2026-09-30 补端口与 userinfo 校验（与 notify._send_wecom 同口径）：白名单
    语义按「主机 + 标准端口」理解，https://白名单主机:8443/ 或携带 userinfo
    （https://user:pass@host/）的变形一律拒绝。注：本函数解析 DNS 校验 IP 后，
    requests 仍会重新独立解析（DNS TOCTOU），彻底闭合需按 IP 直连并固定
    Host/SNI，第 1 层字面量白名单 + 禁重定向是实际控制，此处不再重复加固。
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in allowed_hosts:
        raise ValueError(f"非白名单主机: {parts.hostname!r}")
    if parts.port not in (None, port):
        # 非法端口串（如 :abc）访问 parts.port 时同样抛 ValueError，语义一致
        raise ValueError(f"非白名单端口: {parts.port!r}")
    if parts.username or parts.password:
        raise ValueError("URL 不允许携带 userinfo")
    for info in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if is_restricted_ip(ip):
            raise ValueError(f"目标解析到受限地址: {ip}")


def safe_get(url: str, allowed_hosts, params=None, timeout: float = 20.0):
    """守卫后的 GET：https + 主机白名单 + 受限地址阻断，禁跟随重定向。

    失败分支留现场采样（capture_sample）：网络异常记异常，响应到达但非 200 记
    状态码 + body 头——守卫拦截（check_url 抛 ValueError）不采样，那不是上游的锅。
    """
    check_url(url, allowed_hosts)
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         allow_redirects=False, headers=_UA)
    except Exception as e:  # noqa: BLE001 - 采样后原样上抛
        capture_sample(url, exc=e)
        raise
    if r.status_code != 200:
        capture_sample(url, resp=r)
    return r


# ---------------------------------------------------------------- 上游失败现场采样

_SAMPLE_BODY_HEAD = 2048   # 响应体采样字节数：够辨认限流页/改版页，不拖大落盘
_SAMPLE_KEEP = 50          # 目录滚动上限（.status/upstream/，gitignore 内）


def _sample_dir() -> str:
    """现场采样目录（仓库 .status/upstream/）；测试换桩此函数即可隔离。"""
    return str(Path(__file__).resolve().parents[1] / ".status" / "upstream")


def capture_sample(url: str, *, resp=None, exc=None, note: str = "",
                   body_head: int = _SAMPLE_BODY_HEAD, keep: int = _SAMPLE_KEEP):
    """上游失败现场采样（尽力而为，绝不抛）：状态码/异常 + 响应体前 N 字节落盘。

    两条守卫入口（safe_get / http_retry.guarded_get）中前者已挂钩（2026-10-27 起，
    覆盖 us_market / spec_ohlc_fb 抓取路径）；guarded_get 侧待后续批次；
    us_market.fetch_kline 对「200 但 body 非 JSON」（典型限流页）带 note 主动采。
    文件名带微秒戳防并发覆写（us_market 57 标的多线程同拍失败的场景），写完按
    keep 滚动；链路推送用 latest_sample_note() 取最新一条摘要。返回采样路径，
    采样自身失败返回 None——诊断是副线，绝不反过来打断抓取。写盘走 fsutil
    原子写单一来源（_safe_dst 规范化校验 + 临时文件替换，不留半截文件）。
    """
    try:
        if resp is None and exc is None:
            return None
        host = urlsplit(url).hostname or "unknown"
        safe_host = "".join(c if c.isalnum() or c in "._-" else "_" for c in host)[:40]
        lines = [
            f"time: {_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"url: {url}",
        ]
        if resp is not None:
            lines.append(f"status: {getattr(resp, 'status_code', '?')}")
        if exc is not None:
            lines.append(f"error: {type(exc).__name__}: {exc}")
        if note:
            lines.append(f"note: {note}")
        body = getattr(resp, "content", None) if resp is not None else None
        if body:
            head = bytes(body[:body_head]).decode("utf-8", "replace")
            lines.append(f"--- body head ({min(len(body), body_head)}/{len(body)} bytes) ---")
            lines.append(head)
        d = _sample_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{_dt.datetime.now():%Y%m%d_%H%M%S_%f}_{safe_host}.txt")
        import fsutil  # noqa: PLC0415  延迟导入：netguard 被独立引入时不强拉写盘依赖
        fsutil.save_text_atomic(path, "\n".join(lines) + "\n", newline="\n")
        _prune_samples(d, keep)
        return path
    except Exception:  # noqa: BLE001 - 诊断副线，任何失败不影响主链路
        return None


def _prune_samples(d: str, keep: int) -> None:
    """按文件名（微秒戳前缀，天然按时间序）滚动，只留最新 keep 份。"""
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".txt"))
    except OSError:
        return
    for n in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def latest_sample_note(max_age_hours: float = 24.0) -> str | None:
    """最新一条失败现场的一行摘要（无采样或已超龄返回 None）。供链路失败推送附带。

    us_close_task 的 chain:us-failed 用它把「失败 + 现场」并成一条推送：先看
    status/error 行即可分辨限流页/改版/断网，body 头去 .status/upstream/ 细看。
    """
    try:
        d = _sample_dir()
        names = sorted(n for n in os.listdir(d) if n.endswith(".txt"))
        if not names:
            return None
        name = names[-1]
        path = os.path.join(d, name)
        if time.time() - os.path.getmtime(path) > max_age_hours * 3600:
            return None
        status = err = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("status: "):
                    status = line[8:].strip()
                elif line.startswith("error: "):
                    err = line[7:].strip()
                elif line.startswith("---"):
                    break
        detail = f"status={status}" if status else f"error={(err or '?')[:80]}"
        return f"上游失败现场已采样：.status/upstream/{name}（{detail}）"
    except Exception:  # noqa: BLE001 - 摘要拿不到就当没有，不影响推送主文
        return None


def diagnose(allowed_hosts) -> list[dict]:
    """逐主机报告解析结果与守卫判定，用于区分「守卫误杀」与「源真的不可达」。

    失效曾经只表现为上游几十条同质报错，难以归因；本函数把第 2 层的判定理由摊开。
    """
    out = []
    for host in sorted(allowed_hosts):
        rec = {"host": host, "ips": [], "blocked": [], "ok": False, "error": None}
        try:
            for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
                ip = ipaddress.ip_address(info[4][0])
                if str(ip) in rec["ips"]:
                    continue
                rec["ips"].append(str(ip))
                if is_restricted_ip(ip):
                    rec["blocked"].append(str(ip))
                if is_proxy_fakeip(ip):
                    rec["fakeip"] = True
            rec["ok"] = bool(rec["ips"]) and not rec["blocked"]
        except Exception as e:  # noqa: BLE001 - 诊断函数不抛，如实记录
            rec["error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


if __name__ == "__main__":
    import json
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    hosts = {"web.ifzq.gtimg.cn", "hq.sinajs.cn", "finance.sina.com.cn"}
    print("proxy env:", {k: v for k, v in os.environ.items()
                         if "proxy" in k.lower()})
    print("fake-ip nets:", [str(n) for n in PROXY_FAKEIP_NETS])
    print(json.dumps(diagnose(hosts), ensure_ascii=False, indent=1))
