# -*- coding: utf-8 -*-
"""通过本地 Lumi MCP 直接调用 wencai_query，批量拉取日级资金流（P0 缺口）。

口径：
- 主体：沪深主板（600/601/603/605/000/001/002/003），后续本地再按 panel 过滤
- 字段：主力资金流向 / 特大单净买入额 / 中单净买入额 / 小单净买入额（宽表，每股一行，日期为列）
"""
import json
import sys
from pathlib import Path

import requests

URL = "http://127.0.0.1:58866/mcp"
TOKEN = "8d01869e1354c6cdde40c85e74b099cc8a92924b96df2efc9b799b32f29a0de9"


def _sse_events(text):
    """按 SSE 分隔空行切事件，data 行可能跨多行。"""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        if not data:
            continue
        try:
            out.append(json.loads("\n".join(data)))
        except json.JSONDecodeError:
            continue
    return out


class MCP:
    def __init__(self):
        self.s = requests.Session()
        self.h = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        r = self.s.post(URL, headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "probe", "version": "1"}},
        }, timeout=60)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.h["mcp-session-id"] = sid
        self.s.post(URL, headers=self.h,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=60)
        self.id = 10

    def call(self, name, args, timeout=180):
        self.id += 1
        r = self.s.post(URL, headers=self.h, json={
            "jsonrpc": "2.0", "id": self.id, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }, timeout=timeout)
        raw = r.content.decode("utf-8", errors="replace")
        for obj in _sse_events(raw):
            if "result" in obj:
                return obj["result"]
            if "error" in obj:
                raise RuntimeError(obj["error"])
        raise RuntimeError("no data frame: " + raw[:300])


def unwrap(res):
    """MCP tool result -> 业务 JSON。"""
    parts = res.get("content") or []
    txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return json.loads(txt)


if __name__ == "__main__":
    m = MCP()
    q = ("2026年7月1日至2026年10月30日 各交易日主力资金流向 特大单净买入额 "
         "中单净买入额 小单净买入额")
    out = unwrap(m.call("wencai_query", {"skillId": "hithink-market-query",
                                        "query": q, "limit": 100, "page": 1}))
    d = out.get("data") or out
    print("status", out.get("status_code"))
    print("row_count", d.get("row_count"), "cols", len(d.get("columns") or []))
    print("first row keys", list((d.get("datas") or [{}])[0].keys())[:6])
    print("nfields", len((d.get("datas") or [{}])[0]))
