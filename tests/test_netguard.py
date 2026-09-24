# -*- coding: utf-8 -*-
"""netguard 单测：SSRF 防线（backend/netguard.py）的边界语义固化。

用例矩阵对应《docs/测试与审查方案-20260925.md》§5.1（A 豁免边界 / B 真实私网 /
C 环回与链路本地 / D 保留与组播 / E IPv6 / F 协议与白名单 / G 重定向 / H 诊断）。

纪律（2026-09-25 审查 F3 的教训）：
- A–E 组直接测 netguard.is_restricted_ip 的**自身语义**（/15 豁免精确、其余阻断），
  不经由 ipaddress.is_private 间接断言——后者对 198.18.0.0/15 等段的分类随 Python
  版本漂移（3.13+ 才计入 private），CI(3.11) 与开发机(3.14) 必须双绿；
- 只用跨版本分类稳定的地址；2001:db8::/32、100.64/10 等版本敏感段刻意不入矩阵；
- F–G 组 mock socket.getaddrinfo 与 requests.get，全程零真实网络。
"""
import ipaddress
import socket

import pytest

import netguard

HOSTS = {"hq.sinajs.cn", "web.ifzq.gtimg.cn", "finance.sina.com.cn"}

PUBLIC_V4 = "93.184.216.34"


def _fake_dns(*addrs):
    """伪造 getaddrinfo：把任何主机解析到给定地址序列。"""

    def fake(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 "", (a, port)) for a in addrs]

    return fake


# ---------------------------------------------------------------- A+B+C+D+E 纯函数

def test_a_exemption_exact_15():
    """豁免精确等于 198.18.0.0/15：段内放行（即便 is_private 判 True 的版本上
    也靠豁免过关）；邻段不在豁免网段内，按标准规则处理（公网 → 不受限）。"""
    assert netguard.PROXY_FAKEIP_NETS == (ipaddress.ip_network("198.18.0.0/15"),)
    for s in ("198.18.0.0", "198.18.1.1", "198.19.255.255"):
        assert netguard.is_restricted_ip(ipaddress.ip_address(s)) is False, s
    for s in ("198.17.255.255", "198.20.0.0"):
        ip = ipaddress.ip_address(s)
        assert all(ip not in net for net in netguard.PROXY_FAKEIP_NETS), s
        assert netguard.is_restricted_ip(ip) is False, s   # 本就公网，无需豁免


def test_b_real_private_nets_still_blocked():
    """豁免不得外溢到真实私网。"""
    for s in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        assert netguard.is_restricted_ip(ipaddress.ip_address(s)) is True, s


def test_c_loopback_and_link_local_blocked():
    for s in ("127.0.0.1", "::1", "169.254.1.1", "169.254.169.254"):
        assert netguard.is_restricted_ip(ipaddress.ip_address(s)) is True, s


def test_d_reserved_and_multicast_blocked():
    for s in ("240.0.0.1", "224.0.0.1"):
        assert netguard.is_restricted_ip(ipaddress.ip_address(s)) is True, s


def test_e_ipv6_blocked_and_public_allowed():
    for s in ("fc00::1", "fe80::1"):
        assert netguard.is_restricted_ip(ipaddress.ip_address(s)) is True, s
    assert netguard.is_restricted_ip(ipaddress.ip_address("2606:4700:4700::1111")) is False
    assert netguard.is_restricted_ip(ipaddress.ip_address("8.8.8.8")) is False


# ---------------------------------------------------------------- F 协议/白名单/DNS 层

def test_f1_http_scheme_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(PUBLIC_V4))
    with pytest.raises(ValueError, match="非白名单主机"):
        netguard.check_url("http://hq.sinajs.cn/x", HOSTS)


def test_f2_non_whitelisted_host_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(PUBLIC_V4))
    with pytest.raises(ValueError, match="非白名单主机"):
        netguard.check_url("https://evil.example.com/x", HOSTS)


def test_f3_uppercase_scheme_host_passes(monkeypatch):
    """urlsplit 对 scheme/hostname 小写化：大写形式必须照常放行（固化防回归）。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(PUBLIC_V4))
    netguard.check_url("HTTPS://HQ.SINAJS.CN/quotelist", HOSTS)  # 不抛即过


def test_f4_trailing_dot_hostname_rejected(monkeypatch):
    """尾点域名不是同一主机：fail-closed 拒绝（DNS 安全语义固化）。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(PUBLIC_V4))
    with pytest.raises(ValueError, match="非白名单主机"):
        netguard.check_url("https://hq.sinajs.cn./x", HOSTS)


def test_f5_dns_to_private_ip_blocked(monkeypatch):
    """第 2 层纵深：白名单内主机解析到私网地址仍阻断。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("10.0.0.5", "192.168.1.9"))
    with pytest.raises(ValueError, match="目标解析到受限地址"):
        netguard.check_url("https://hq.sinajs.cn/x", HOSTS)


def test_f6_dns_to_fakeip_range_allowed(monkeypatch):
    """原始 bug 的回归锚：fake-IP 段解析结果必须放行（2026-09-24 前此处整体失效）。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("198.18.0.206"))
    netguard.check_url("https://hq.sinajs.cn/x", HOSTS)  # 不抛即过


# ---------------------------------------------------------------- G safe_get 语义

def test_g1_safe_get_no_redirect_and_ua(monkeypatch):
    """allow_redirects=False 是防线一部分（重定向到内网是经典绕过），参数固化。"""
    seen = {}
    sentinel = object()

    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(PUBLIC_V4))
    monkeypatch.setattr(netguard.requests, "get", fake_get)
    out = netguard.safe_get("https://hq.sinajs.cn/x", HOSTS, params={"a": 1}, timeout=3)
    assert out is sentinel
    assert seen["url"] == "https://hq.sinajs.cn/x"
    assert seen["allow_redirects"] is False
    assert seen["timeout"] == 3
    assert seen["params"] == {"a": 1}
    assert "User-Agent" in seen["headers"]


def test_g2_safe_get_propagates_guard_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("127.0.0.1"))
    with pytest.raises(ValueError):
        netguard.safe_get("https://hq.sinajs.cn/x", HOSTS)


# ---------------------------------------------------------------- H 诊断

def test_h1_diagnose_reports_error_without_raising(monkeypatch):
    def boom(host, port, proto=None):
        raise OSError("unresolvable")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    rec = netguard.diagnose({"down.example.com"})[0]
    assert rec["ok"] is False
    assert "OSError" in rec["error"]


def test_h2_diagnose_flags_fakeip_and_dedupes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("198.18.0.206", "198.18.0.206"))
    rec = netguard.diagnose({"hq.sinajs.cn"})[0]
    assert rec["ok"] is True
    assert rec["blocked"] == []
    assert rec["fakeip"] is True
    assert rec["ips"] == ["198.18.0.206"]      # 同 IP 去重


def test_h3_diagnose_mixed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("198.18.0.1", "10.1.2.3"))
    rec = netguard.diagnose({"hq.sinajs.cn"})[0]
    assert rec["ok"] is False                   # 有一个受限解析即整体不可信
    assert rec["blocked"] == ["10.1.2.3"]


# ---------------------------------------------------------------- I notify 调用点复用（F8）

def test_i1_notify_post_allows_fakeip_resolution(monkeypatch):
    """F8 回归锚：fake-IP DNS 下通知推送不得被 is_global 检查误拒
    （2026-09-24 us_market 同款病灶的第三处副本，修复于 2026-09-25）。"""
    import notify
    seen = {}
    sentinel = object()

    def fake_post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("198.18.0.206"))
    monkeypatch.setattr(notify.requests, "post", fake_post)
    resp, err = notify._post("sctapi.ftqq.com", "/push", data={"x": 1})
    assert err is None and resp is sentinel
    assert seen["url"] == "https://sctapi.ftqq.com/push"
    assert seen["allow_redirects"] is False


def test_i2_notify_post_still_blocks_private(monkeypatch):
    import notify
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("10.0.0.5"))
    resp, err = notify._post("sctapi.ftqq.com", "/push")
    assert resp is None
    assert "非公网地址" in err
