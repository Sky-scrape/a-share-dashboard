# -*- coding: utf-8 -*-
"""hithink-finance CLI 封装（subprocess）。

统一入口：
    ht.ht(*args)                 执行命令，返回 envelope 的 data 部分
    ht.date_ms(date8)            8位日期 -> Asia/Shanghai 零点毫秒（池子的 --date-ms）
    ht.pool_all(base_args)       分页聚合 special 池子
    ht.market_snapshot_all()     全市场快照（offset 兜底分页）
    ht.index_snapshot_batches()  批量指数快照（自动分批）

失败抛 RuntimeError（带 error.code），由 providers._err 降级为模块 error。
"""
import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

_EXE = None


def _exe():
    """定位 hithink-finance 可执行文件（Windows npm 全局是 .cmd）。"""
    global _EXE
    if _EXE is None:
        _EXE = shutil.which("hithink-finance") or "hithink-finance.cmd"
    return _EXE


def date_ms(date8):
    """8 位日期（如 20260828）-> Asia/Shanghai 当日零点的毫秒时间戳。"""
    dt = datetime.datetime.strptime(str(date8), "%Y%m%d")
    return int(dt.timestamp() * 1000)


def ht(*args, timeout=90):
    """执行 hithink-finance 命令，成功返回 data 部分；失败抛 RuntimeError。"""
    cmd = [_exe(), *args, "--format", "json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ht timeout: {' '.join(args)} 超过 {timeout}s")
    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"ht JSON 解析失败: {(p.stdout or p.stderr or '')[:200]}")
    if not env.get("ok"):
        err = env.get("error") or {}
        raise RuntimeError(
            f"ht {' '.join(args[:2])} 失败: {err.get('code')}: "
            f"{str(err.get('message'))[:200]}")
    return env.get("data")


def pool_all(base_args, size=200, max_pages=60):
    """分页聚合 special 池子（--page/--size），返回完整 item 列表。

    base_args 形如 ["special", "limit-up-pool", "--date-ms", "1787846400000"]。
    """
    items, page = [], 1
    while page <= max_pages:
        d = ht(*base_args, "--page", str(page), "--size", str(size))
        batch = d.get("item") or []
        items.extend(batch)
        pages = ((d.get("pagination") or {}).get("pages")) or 1
        if page >= pages or not batch:
            break
        page += 1
    return items


def market_snapshot_all(limit=6000, timeout=120):
    """全市场快照（单次大 limit + offset 兜底分页），返回 item 列表。"""
    d = ht("market", "snapshot", "--limit", str(limit), timeout=timeout)
    items = d.get("item") or []
    total = d.get("total") or len(items)
    offset = len(items)
    while offset < total:
        d2 = ht("market", "snapshot", "--limit", str(min(1000, total - offset)),
                "--offset", str(offset), timeout=timeout)
        batch = d2.get("item") or []
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
    return items


def index_snapshot_batches(codes, batch=90, timeout=120):
    """批量 index snapshot（URL 长度安全分批），返回 {thscode: row}。"""
    out = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        d = ht("index", "snapshot", "--thscodes", ",".join(chunk),
               timeout=timeout)
        for x in (d.get("item") or []):
            out[x.get("thscode")] = x
    return out


def catalog(tag, cache_dir=None, cache_days=7):
    """index catalog（tag: industry/cn_concept/tszs），可选本地缓存，返回 item 列表。"""
    cache_file = (os.path.join(cache_dir, f"ht_catalog_{tag}.json")
                  if cache_dir else None)
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                saved = json.load(f)
            if time.time() - saved.get("ts", 0) < cache_days * 86400:
                return saved["items"]
        except Exception:  # noqa: BLE001 - 缓存坏了就重新拉
            pass
    d = ht("index", "catalog", "--tag", tag)
    items = d.get("item") or []
    if cache_file and items:
        os.makedirs(cache_dir, exist_ok=True)
        Path(cache_file).write_text(
            json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False),
            encoding="utf-8")
    return items


def symbol_names(cache_dir=None, cache_days=1):
    """symbol.list 全量 ticker->name 映射（当日缓存），名称兜底用。"""
    cache_file = (os.path.join(cache_dir, "ht_symbol_names.json")
                  if cache_dir else None)
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                saved = json.load(f)
            if time.time() - saved.get("ts", 0) < cache_days * 86400:
                return saved["names"]
        except Exception:  # noqa: BLE001
            pass
    names, offset = {}, 0
    for _ in range(20):
        d = ht("symbol", "list", "--limit", "500", "--offset", str(offset))
        batch = d.get("item") or []
        for x in batch:
            names[str(x.get("ticker"))] = x.get("name")
        if not batch or len(batch) < 500:
            break
        offset += len(batch)
    if cache_file and names:
        os.makedirs(cache_dir, exist_ok=True)
        Path(cache_file).write_text(
            json.dumps({"ts": time.time(), "names": names}, ensure_ascii=False),
            encoding="utf-8")
    return names
