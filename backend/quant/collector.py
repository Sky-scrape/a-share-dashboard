# -*- coding: utf-8 -*-
"""量化平台板块采集层：只读聚合内置引擎 quant/ 的落盘产物 → 总览面板 dict。

原则：
- 只读不写：任务/日志等新增产物落在 A 侧 data/quant/；引擎目录由 quant_sim 自管。
- HTTP 层（server.py）不做业务解析；聚合在这里，快查回测在 quant_api.py。

产出结构（/api/quant）：
{
  "generated_at": "...",
  "platform": {root, exists, name, docs},
  "strategies": [{name, kind, version, updated_at}],        # quant/strategies_store/*.json
  "backtests":  [{time,title,strategy,symbols,window,cum_ret,cagr,max_dd,sharpe,trades}],
  "reports":    [{name, mtime, window, cum_ret, sharpe, max_dd, trades, benchmark, html}],
  "research":   [{name, mtime, rows}],                        # quant/results/research/*.csv
  "signals":    [{name, mtime}],                              # quant/results/signals/*.md
  "ledger":     [{name, mtime}],                              # quant/data/ledger/*
}
"""
import csv
import glob
import json
import os
import time

from quant_config import QUANT_ROOT, MAX_BACKTEST_LOG, MAX_ITEMS_PER_DIR


def _mtime_str(path):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def _dir_files(root, pattern):
    """按修改时间倒序返回绝对路径列表（外部目录不存在则空）。"""
    if not os.path.isdir(root):
        return []
    files = glob.glob(os.path.join(root, pattern))
    try:
        files.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        pass
    return files[:MAX_ITEMS_PER_DIR]


def _num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


# ---------------- 各区块 ----------------

def collect_platform():
    exists = os.path.isdir(QUANT_ROOT)
    docs = []
    if exists:
        for name in ("README.md", "docs/design.md", "docs/changelog.md"):
            p = os.path.join(QUANT_ROOT, name)
            if os.path.isfile(p):
                docs.append({"name": os.path.basename(name), "path": name,
                             "mtime": _mtime_str(p)})
    return {
        "root": QUANT_ROOT,
        "exists": exists,
        "name": "量化引擎（A股/场内ETF · 日线回测 · 内置于看板）",
        "docs": docs,
    }


def collect_strategies():
    """策略库存档 strategies_store/*.json（规则/代码/内置三类统一存档）。"""
    out = []
    for p in _dir_files(os.path.join(QUANT_ROOT, "strategies_store"), "*.json"):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            d = {}
        name = os.path.basename(p)[:-5]
        spec = d.get("spec") if isinstance(d.get("spec"), dict) else d
        kind = d.get("kind") or (("code" if "code" in spec else "rule")
                                 if isinstance(spec, dict) else "存档")
        out.append({
            "name": name,
            "kind": kind,
            "version": d.get("version") or (spec or {}).get("version"),
            "note": (d.get("note") or "")[:120],
            "updated_at": d.get("updated_at") or d.get("created_at") or _mtime_str(p),
        })
    return out


def collect_backtests():
    """results/backtest_log.jsonl 研究流水（UTF-8，逐行 JSON）。"""
    fp = os.path.join(QUANT_ROOT, "results", "backtest_log.jsonl")
    rows = []
    if not os.path.isfile(fp):
        return rows
    try:
        with open(fp, encoding="utf-8-sig") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
    except Exception:
        return rows
    for line in lines[-MAX_BACKTEST_LOG:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        rows.append({
            "time": d.get("time") or "",
            "store": str(d.get("store") or "")[:40],
            "title": d.get("title") or "",
            "strategy": d.get("strategy") or "",
            "symbols": d.get("symbols") or [],
            "window": d.get("window") or "",
            "cum_ret": _num(d.get("cum_ret")),
            "cagr": _num(d.get("cagr")),
            "max_dd": _num(d.get("max_dd")),
            "sharpe": _num(d.get("sharpe")),
            "trades": _num(d.get("trades"), int),
        })
    return rows


_METRIC_KEYS = {  # 中文指标名 → 输出字段（量化平台 report 显示口径的唯一中文键）
    "累计收益率": "cum_ret", "年化收益率": "cagr", "夏普比率": "sharpe",
    "最大回撤": "max_dd", "交易次数": "trades", "胜率": "win_rate",
}


def collect_reports():
    """results/*_metrics.json + 同名 _report.html（自包含回测报告）。"""
    out = []
    for p in _dir_files(os.path.join(QUANT_ROOT, "results"), "*_metrics.json"):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        m = d.get("metrics") or {}
        cfg = d.get("config") or {}
        row = {"name": os.path.basename(p)[:-13], "mtime": _mtime_str(p),
               "window": m.get("回测区间") or ""}
        for zh, key in _METRIC_KEYS.items():
            row[key] = _num(m.get(zh), int if key == "trades" else float)
        row["benchmark"] = cfg.get("benchmark") or ""
        html = os.path.join(os.path.dirname(p), row["name"] + "_report.html")
        row["html"] = os.path.basename(html) if os.path.isfile(html) else ""
        out.append(row)
    return out


def _count_csv_rows(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return max(0, sum(1 for _ in csv.DictReader(f)))
    except Exception:
        return None


def collect_research():
    """results/research/*.csv 参数网格 / Walk-Forward 折与 OOS 拼接。"""
    out = []
    for p in _dir_files(os.path.join(QUANT_ROOT, "results", "research"), "*.csv"):
        out.append({"name": os.path.basename(p), "mtime": _mtime_str(p),
                    "rows": _count_csv_rows(p)})
    return out


def collect_signals():
    """results/signals/*.md 今日信号（明日开盘执行清单）历史归档。"""
    out = []
    for p in _dir_files(os.path.join(QUANT_ROOT, "results", "signals"), "*.md"):
        out.append({"name": os.path.basename(p), "mtime": _mtime_str(p)})
    return out


def collect_ledger():
    """data/ledger/* 成交轻台账。"""
    out = []
    root = os.path.join(QUANT_ROOT, "data", "ledger")
    for p in _dir_files(root, "*.json") + _dir_files(root, "*.csv"):
        out.append({"name": os.path.basename(p), "mtime": _mtime_str(p)})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:MAX_ITEMS_PER_DIR]


# ---------------- 总入口 ----------------

def collect():
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": collect_platform(),
        "strategies": collect_strategies(),
        "backtests": collect_backtests(),
        "reports": collect_reports(),
        "research": collect_research(),
        "signals": collect_signals(),
        "ledger": collect_ledger(),
    }
