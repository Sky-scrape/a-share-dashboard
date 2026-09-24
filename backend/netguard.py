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
"""
from __future__ import annotations

import ipaddress
import socket
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
    """校验协议与主机白名单，并阻断解析到受限地址的目标。不通过抛 ValueError。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in allowed_hosts:
        raise ValueError(f"非白名单主机: {parts.hostname!r}")
    for info in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if is_restricted_ip(ip):
            raise ValueError(f"目标解析到受限地址: {ip}")


def safe_get(url: str, allowed_hosts, params=None, timeout: float = 20.0):
    """守卫后的 GET：https + 主机白名单 + 受限地址阻断，禁跟随重定向。"""
    check_url(url, allowed_hosts)
    return requests.get(url, params=params, timeout=timeout,
                        allow_redirects=False, headers=_UA)


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
