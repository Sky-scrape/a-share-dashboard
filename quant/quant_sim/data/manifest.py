"""行情落盘的「数据身份证」：目录级 `_manifest.json`，记录每个标的文件的口径与元信息。

坑的来历（8/29 设计评审）：Parquet 落盘不携带复权口径——同目录下混着手放 raw CSV 时，
涨跌停判定（依赖 pre_close）与信号会整体错位且无任何告警；同一段历史在不同时间点
因分红被 qfq 改写后，两次导出结果悄悄不同。复权口径是「信任地基」，必须随数据落盘。

布局：与被导出文件同目录的 `_manifest.json`：
    {"<symbol>.parquet": {"symbol","adjust","source","exported_at","rows","first","last", ...}}
读侧：loader.load_panel 自动附带 panel.metadata["manifest"]；expect_adjust 不一致的标的
进 panel.metadata["manifest_mismatch"]，由 UI/报告醒目告警——不静默使用。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

MANIFEST_NAME = "_manifest.json"


def manifest_path(dir_path: str) -> str:
    return os.path.join(dir_path, MANIFEST_NAME)


def read_manifest(dir_path: str) -> Dict[str, dict]:
    p = manifest_path(dir_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # 坏 manifest 不阻断读取，校验环节会提示


def update_manifest(dir_path: str, entries: Dict[str, dict]) -> None:
    """增量合并写入（读-改-写；多进程并发导出同一目录非本模块支持场景）。"""
    if not entries:
        return
    p = manifest_path(dir_path)
    merged = read_manifest(dir_path)
    for fname, meta in entries.items():
        merged[fname] = meta
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        pass  # 只读目录等极端情况：数据本身已落盘，不因元信息失败而炸导出


def entry_for(frame, *, adjust: str, source: str, **extra) -> dict:
    """从导出的 DataFrame 提取标准元信息。"""
    e = {
        "adjust": adjust,
        "source": source,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": int(len(frame)),
        "first": str(frame.index.min().date()),
        "last": str(frame.index.max().date()),
    }
    e.update({k: v for k, v in extra.items() if v not in (None, "", 0, {})})
    return e


def check_adjust(manifest: Dict[str, dict], expect_adjust: Optional[str]) -> Dict[str, str]:
    """返回个股口径不一致清单 {file: "实际口径"}。

    只校验同词表（qfq/hfq/raw）的个股文件：ETF 的 dividend_reinvested、指数的 none
    是各自链路的固有口径，不是错配，不参与股票口径比较（否则混装面板全是误报）。
    无/未知口径的文件不判（兼容手工放的非 hithink 数据）。
    """
    if not expect_adjust or not manifest:
        return {}
    stock_vocab = {"qfq", "hfq", "raw"}
    want = str(expect_adjust).lower()
    if want not in stock_vocab:
        return {}
    out: Dict[str, str] = {}
    for fname, meta in manifest.items():
        got = str((meta or {}).get("adjust", "")).lower()
        if got in stock_vocab and got != want:
            out[fname] = got
    return out
