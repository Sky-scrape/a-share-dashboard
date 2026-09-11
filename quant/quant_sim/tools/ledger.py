"""信号→执行轻台账：为每个存档策略记一本「我实际做了什么」的流水账。

设计取舍（PM 评审确认的轻量方案）：不做完整 paper trading 账户，只记成交。
每策略一个 JSON 文件 `data/ledger/<安全名>.json`，条目：
    {"date","symbol","side":"buy|sell","qty","price","note"}
派生视图：
  * positions_from_entries → 净持仓 + 移动加权成本（卖出多于持仓会截断并警告）
  * rebalance_gap → 台账实际持仓 vs 策略目标持仓 的差额清单（你要补/砍多少）

CLI：
  python -m quant_sim.tools.ledger add --name 策略名 --symbol 159915 --side buy --qty 1000 --price 2.35
  python -m quant_sim.tools.ledger list --name 策略名
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, List, Optional

import pandas as pd

from .. import paths as _paths
from ..core.fsutil import save_json_atomic
from ..strategies.store import _safe


def _dir(ledger_dir: Optional[str]) -> str:
    if ledger_dir:
        return _paths.resolve(ledger_dir)
    return _paths.ledger_dir()


#: 台账名黑名单：路径分隔符、Windows 保留字符、空白、控制字符、DEL
_LEDGER_BAD_CHARS = set('/\\:*?"<>|') | {"\t", "\n", "\r", "\v", "\f"}
#: Windows 保留设备名（不分大小写、不带扩展名也保留）：CON NUL COM1… 会裸盘失败
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def _validate_name(name: str) -> str:
    """台账名安全校验（**黑名单制**）：防路径穿越与危险文件名，同时放行中文等全角字符。

    曾用 [A-Za-z0-9_-] 白名单把中文台账名整体拒掉——中文支持是规格不是漏洞。
    文件名安全由两层共同保证：这里拒绝危险字符，落盘时再经 store._safe 清洗。
    拒绝：/ \\ : * ? " < > |、任何空白与控制字符、"."/".." 及以 "." 开头的名字、
    Windows 保留设备名（con/nul/com1…，2026-09-11 补：这类名落盘会直接失败）。
    """
    s = str(name)
    if not s or s in (".", "..") or s.startswith("."):
        raise ValueError(f"非法 ledger 名称: {name!r}")
    if s.lower().split(".")[0] in _WINDOWS_RESERVED:
        raise ValueError(f"非法 ledger 名称（Windows 保留设备名）: {name!r}")
    for ch in s:
        if ch in _LEDGER_BAD_CHARS or ch.isspace() or ord(ch) < 32 or ord(ch) == 127:
            raise ValueError(f"非法 ledger 名称: {name!r}")
    return s


def _path(name: str, ledger_dir: Optional[str] = None) -> str:
    """只解析路径，不建目录（load/list 等读操作不得有写盘副作用）。"""
    return os.path.join(_dir(ledger_dir), f"{_safe(name)}.json")


def _path_rw(name: str, ledger_dir: Optional[str] = None) -> str:
    d = _dir(ledger_dir)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_safe(name)}.json")


def load_ledger(name: str, ledger_dir: Optional[str] = None) -> List[dict]:
    _validate_name(name)   # 与写入口径一致：坏名直接报错，不静默当空台账
    p = _path(name, ledger_dir)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def add_entry(name: str, symbol: str, side: str, qty: float, price: float,
              date: str = "", note: str = "", ledger_dir: Optional[str] = None) -> dict:
    """记一笔成交。side: buy|sell；date 缺省为今天。"""
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError("side 必须是 buy 或 sell")
    if qty <= 0 or price <= 0:
        raise ValueError("qty 与 price 必须为正数")
    entry = {"date": date or time.strftime("%Y-%m-%d"), "symbol": str(symbol), "side": side,
             "qty": float(qty), "price": float(price), "note": note,
             "logged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _validate_name(name)
    data = load_ledger(name, ledger_dir)
    data.append(entry)
    save_json_atomic(_path_rw(name, ledger_dir), data, indent=1)
    return entry


def undo_last(name: str, ledger_dir: Optional[str] = None) -> Optional[dict]:
    _validate_name(name)
    data = load_ledger(name, ledger_dir)
    if not data:
        return None
    last = data.pop()
    save_json_atomic(_path_rw(name, ledger_dir), data, indent=1)
    return last


def clear_ledger(name: str, ledger_dir: Optional[str] = None) -> bool:
    _validate_name(name)
    p = _path(name, ledger_dir)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def positions_from_entries(entries: List[dict]) -> pd.DataFrame:
    """流水 → 净持仓表（symbol/qty/avg_cost/fees_ignored）。移动加权成本。

    卖出数量超过当前持仓时截断到持仓并打警告（台账录错了要能发现，不能负持仓）。
    """
    pos: Dict[str, dict] = {}
    warnings: List[str] = []
    for e in sorted(entries, key=lambda x: str(x.get("date", ""))):
        s = str(e["symbol"])
        q = float(e["qty"])
        p = float(e["price"])
        cur = pos.setdefault(s, {"qty": 0.0, "avg_cost": 0.0})
        if e["side"] == "buy":
            tot = cur["qty"] + q
            cur["avg_cost"] = (cur["qty"] * cur["avg_cost"] + q * p) / tot if tot > 0 else p
            cur["qty"] = tot
        else:
            if q > cur["qty"] + 1e-9:
                warnings.append(f"{e.get('date','?')} 卖出 {s} {q:g} 超过持仓 {cur['qty']:g}，已截断")
                q = cur["qty"]
            cur["qty"] -= q
            if cur["qty"] <= 1e-9:
                cur["qty"] = 0.0
                cur["avg_cost"] = 0.0
    rows = [{"symbol": s, "qty": v["qty"], "avg_cost": round(v["avg_cost"], 4)}
            for s, v in sorted(pos.items()) if v["qty"] > 1e-9]
    df = pd.DataFrame(rows, columns=["symbol", "qty", "avg_cost"])
    df.attrs["warnings"] = warnings
    return df


def mark_to_market(positions: pd.DataFrame, last_prices: Dict[str, float]) -> pd.DataFrame:
    """持仓 + 最新收盘价 → 市值/浮动盈亏表（缺价标的按成本价估，盈亏 0）。"""
    out = positions.copy()
    out["last"] = out.apply(
        lambda r: float(last_prices.get(str(r["symbol"]), 0.0) or float(r["avg_cost"])), axis=1)
    out["市值"] = out["qty"] * out["last"]
    out["浮盈"] = out["qty"] * (out["last"] - out["avg_cost"])
    out["收益率"] = out.apply(lambda r: (r["last"] / r["avg_cost"] - 1) if r["avg_cost"] > 0 else 0.0, axis=1)
    return out


def rebalance_gap(ledger_pos: pd.DataFrame, target_qty: Dict[str, float]) -> pd.DataFrame:
    """台账实际 vs 策略目标 → 差额清单（正=需买入，负=需卖出）。"""
    syms = sorted(set(ledger_pos["symbol"].map(str)) | set(map(str, target_qty.keys())))
    have = {str(r.symbol): float(r.qty) for r in ledger_pos.itertuples()}
    rows = []
    for s in syms:
        want = float(target_qty.get(s, 0.0))
        got = have.get(s, 0.0)
        gap = want - got
        if abs(gap) < 1e-6:
            continue
        rows.append({"symbol": s, "策略目标": want, "台账实际": got, "差额": gap,
                     "动作": "买入" if gap > 0 else "卖出"})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="策略成交轻台账")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="记一笔成交")
    a.add_argument("--name", required=True)
    a.add_argument("--symbol", required=True)
    a.add_argument("--side", required=True, choices=["buy", "sell"])
    a.add_argument("--qty", required=True, type=float)
    a.add_argument("--price", required=True, type=float)
    a.add_argument("--date", default="")
    a.add_argument("--note", default="")
    l = sub.add_parser("list", help="查看台账与当前持仓")
    l.add_argument("--name", required=True)
    u = sub.add_parser("undo", help="撤销最后一笔")
    u.add_argument("--name", required=True)
    args = ap.parse_args()

    if args.cmd == "add":
        e = add_entry(args.name, args.symbol, args.side, args.qty, args.price, date=args.date, note=args.note)
        print(f"已记录：{e['date']} {'买入' if e['side']=='buy' else '卖出'} {e['symbol']} "
              f"{e['qty']:g} 股 @ {e['price']:g}")
    else:
        entries = load_ledger(args.name)
        if not entries:
            print("（台账为空）")
            return
        print(pd.DataFrame(entries)[["date", "symbol", "side", "qty", "price", "note"]].to_string(index=False))
        pos = positions_from_entries(entries)
        print("\n当前持仓：")
        print(pos.to_string(index=False) if len(pos) else "（空仓）")
        for w in pos.attrs.get("warnings", []):
            print(f"⚠️ {w}")
    if args.cmd == "undo":
        e = undo_last(args.name)
        print(f"已撤销：{e}" if e else "没有可撤销的记录")


if __name__ == "__main__":
    main()
