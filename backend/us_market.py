# -*- coding: utf-8 -*-
"""美股隔夜数据层 · 单一来源（2026-09-08 新增）。

把美股「大盘 / 板块 / 个股」表现对齐为 A 股交易日因子，供：
  - 复盘备选池（backend/recap/speculate.py：环境闸门 + 候选打分）
  - 策略自迭代引擎（strategy-iter：规则版本因子）
共用同一份 factors.json，概念→美股代理映射也从本模块 import（单一来源）。

时序口径（防未来函数）：
  A 股交易日 T 的「隔夜美股」= 最近一个在 T+1 日 09:15（北京时间）前收盘的
  美股交易日 U = 最大的「≤T」美股交易日（C7 新时序：复盘在 T+1 凌晨美股收盘
  后完成，U=T 场次已于 T+1 04:00/05:00 收盘，严格先于 T+1 开盘）。
  factors.json 的键永远是 A 股交易日 T，行内 us_date 记录实际对齐的美股日期。
  未收盘的美股 bar（美国东部 16:15 前）一律不写入历史、不参与因子。

数据源：腾讯 usfqkline（web.ifzq.gtimg.cn，一次请求拉全量前复权日线）。
  - 大盘：IXIC 纳斯达克综合 + SPY（标普500 代理）+ DJI 道琼斯；
    东财 100.NDX 是纳指100 口径且无综合指数，故纳指取腾讯 IXIC。
  - 板块：SPDR 11 行业 ETF + SMH 半导体，按收盘价等权算涨跌与上涨占比。
  - 个股：按 A 股概念分组的美股代理篮子（US_PROXY_GROUPS）。
选股/打分只用 close 口径（涨跌幅、5 日、组内均值），不依赖 OHLC 细节。

文件布局（data/recap/us_market/，全部字面量文件名）：
  hist_all.json     全符号日线 {"symbols": {SYM: {name,kind,rows:[[date,o,c,h,l,v],...]}}}
  ashare_cal.json   A 股交易日历缓存（akshare 新浪口径，7 天刷新）
  factors.json      {rows: {A股T: 因子行}} —— 引擎与线上池的唯一消费入口

用法：
  python backend/us_market.py                      # 每日增量抓取 + 重建因子
  python backend/us_market.py --backfill 20250901  # 全量回补
  python backend/us_market.py --factors-only       # 只重建因子（不发请求）
  python backend/us_market.py --status             # 各符号覆盖状态
"""
from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402
import fsutil    # noqa: E402  原子写盘单一来源（backend/fsutil.py）
from http_retry import retry  # noqa: E402

DATA_DIR = Path(os.path.abspath(os.path.join(os.path.dirname(HERE),
                                             "data", "recap", "us_market")))
HIST_FILE = "hist_all.json"
CAL_FILE = "ashare_cal.json"
FACTOR_FILE = "factors.json"

HIST_START = "2025-09-01"    # 回补起点（2026-01-01 验证区间前留足 20 日趋势暖机）
FACTOR_START = "2025-11-03"  # 因子输出起点（与 strategy-iter 暖机窗口一致）
CAL_TTL_DAYS = 7             # A 股日历缓存刷新周期
REQUEST_INTERVAL = 0.6       # 逐符号请求间隔（秒），避免触发限流
KLINE_COUNT_BACKFILL = 800   # 一次请求可拿全量（日线 ~250 根/年）

KLINE_HOST = "web.ifzq.gtimg.cn"           # 固定主机，不从外部输入拼协议/域名
# 腾讯端点对美股 ETF 只保留最近 1 根 bar，板块 ETF 改走新浪静态页（fetch_global 同源）
SINA_HOST = "finance.sina.com.cn"
_ALLOWED_HOSTS = frozenset({KLINE_HOST, SINA_HOST})
_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,8}(\.[A-Za-z0-9]{1,4})?$")  # us 前缀+代码(.后缀)
_SINA_SYM_RE = re.compile(r"^[a-z]{1,8}$")                        # 新浪 ETF 小写代码

# ---- 大盘基准（sym, 简称）——纳指综合口径，标普以 SPY 代理 ----
US_BENCH = [
    ("usIXIC", "IXIC", "纳斯达克综合"),
    ("usSPY", "SPY", "标普500"),
    ("usDJI", "DJI", "道琼斯"),
]

# ---- 板块 ETF：SPDR 11 行业 + 半导体（收盘价等权口径）----
US_SECTORS = [
    ("usXLK", "XLK", "科技"), ("usXLF", "XLF", "金融"), ("usXLV", "XLV", "医疗"),
    ("usXLE", "XLE", "能源"), ("usXLY", "XLY", "可选消费"), ("usXLP", "XLP", "必需消费"),
    ("usXLI", "XLI", "工业"), ("usXLB", "XLB", "材料"), ("usXLU", "XLU", "公用"),
    ("usXLRE", "XLRE", "地产"), ("usXLC", "XLC", "通信"), ("usSMH", "SMH", "半导体"),
]

# ---- 概念 → 美股代理篮子（匹配顺序即优先级：先专用后泛化）----
# 关键词匹配 A 股概念名（子串包含）；命中取组内等权隔夜涨跌幅。
US_PROXY_GROUPS = {
    "半导体链": ["存储", "晶圆", "封测", "光刻", "芯片", "半导体", "电子化学品"],
    "AI算力": ["算力", "AI", "人工智能", "CPO", "光模块", "光通信", "数据中心",
              "液冷", "东数西算", "AIGC", "大模型", "英伟达", "GPU", "IDC"],
    "消费电子": ["消费电子", "苹果", "果链", "智能穿戴", "无线耳机", "折叠屏"],
    "中概互联": ["中概", "跨境电商", "互联网", "电商", "平台经济", "网络游戏", "在线教育"],
    "美股科技": ["软件", "云计算", "信息安全", "信创"],
    "新能源车": ["新能源车", "锂电", "固态电池", "智能驾驶", "无人驾驶", "充电桩",
               "汽车零部件", "汽车整车"],
    "机器人": ["机器人", "减速器", "执行器", "谐波", "伺服"],
    "创新药": ["创新药", "生物医药", "减肥药", "CXO", "疫苗", "医疗器械"],
    "军工航空": ["军工", "国防", "航天", "航空", "大飞机", "无人机", "导弹", "商业航天"],
    "黄金有色": ["黄金", "贵金属", "有色", "铜", "铝", "小金属"],
    "加密货币": ["数字货币", "加密货币", "区块链", "稳定币"],
    "油气": ["油气", "石油", "天然气", "油服", "页岩气"],
    "光伏储能": ["光伏", "储能", "逆变器"],
    "金融": ["银行", "证券", "保险", "金融科技", "多元金融"],
}

US_PROXY_SYMS = {
    "半导体链": ["usMU.OQ", "usTSM.N", "usASML.OQ", "usINTC.OQ", "usQCOM.OQ"],
    "AI算力": ["usNVDA.OQ", "usAMD.OQ", "usAVGO.OQ", "usSMCI.OQ", "usORCL.N", "usMSFT.OQ"],
    "消费电子": ["usAAPL.OQ"],
    "中概互联": ["usBABA.N", "usPDD.OQ", "usJD.OQ", "usBIDU.OQ", "usNTES.OQ", "usTCOM.OQ"],
    "美股科技": ["usGOOGL.OQ", "usAMZN.OQ", "usMETA.OQ", "usNFLX.OQ"],
    "新能源车": ["usTSLA.OQ"],
    "机器人": ["usTSLA.OQ", "usNVDA.OQ"],
    "创新药": ["usLLY.N", "usNVO.N", "usPFE.N", "usMRK.N"],
    "军工航空": ["usLMT.N", "usRTX.N", "usNOC.N", "usBA.N"],
    "黄金有色": ["usNEM.N", "usFCX.N"],
    "加密货币": ["usCOIN.OQ", "usMSTR.OQ"],
    "油气": ["usXOM.N", "usCVX.N"],
    "光伏储能": ["usFSLR.OQ", "usENPH.OQ"],
    "金融": ["usJPM.N", "usGS.N", "usBAC.N"],
}

# 概念名 → 代理组（匹配表；US_PROXY_GROUPS 顺序即优先级）
CONCEPT_GROUP_TABLE: list[tuple[str, list[str]]] = list(US_PROXY_GROUPS.items())

# 腾讯端点对美股 ETF 无历史，这些符号走新浪静态页（usXLK 等板块 ETF + SPY）
_SINA_SOURCE_SYMS = frozenset(
    {s for s, _k, _n in US_SECTORS} | {"usSPY"})


def match_proxy_group(concept_name):
    """A 股概念名 → 美股代理组名；无命中返回 None（诚实缺失，不打分）。"""
    if not concept_name:
        return None
    for grp, kws in CONCEPT_GROUP_TABLE:
        for kw in kws:
            if kw in concept_name:
                return grp
    return None


# ---------------------------------------------------------------- 安全边界

def _safe_sym(sym: str) -> str:
    """符号白名单校验：只允许字母数字与一个点后缀（防 URL/JSON 键注入）。"""
    s = str(sym)
    if not _SYM_RE.match(s):
        raise ValueError(f"非法符号名: {sym!r}")
    return s


def _safe_get(url: str, timeout: float = 20.0):
    """仅允许 https + 白名单主机；解析 DNS 并阻断私网/环回/链路本地地址。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"非白名单主机: {parts.hostname!r}")
    for info in socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise ValueError(f"目标解析到受限地址: {ip}")
    return requests.get(url, timeout=timeout, allow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0"})


# ---------------------------------------------------------------- 落盘（字面量文件名 + 原子写）

def _write_json(fname: str, payload: dict) -> None:
    """写 data/recap/us_market/ 下的白名单文件（fname 必须是模块内字面量）。

    hist_all.json 全库单文件、factors.json 被 server/speculate 随时读——都走
    backend/fsutil 原子写（临时文件 + os.replace），读者永远不会看到半截。"""
    if fname not in (HIST_FILE, CAL_FILE, FACTOR_FILE):
        raise ValueError(f"非法文件名: {fname!r}")
    fsutil.save_json_atomic(DATA_DIR / fname, payload,
                            separators=(",", ":"))


def _read_json(fname: str) -> dict | None:
    try:
        with open(DATA_DIR / fname, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - 缺失/损坏按空处理（下次全量自愈）
        return None


def load_hist_all() -> dict:
    """全符号日线 {sym: {name, kind, rows:[[date,o,c,h,l,v],...]}}；缺失返回 {}。"""
    d = _read_json(HIST_FILE) or {}
    return d.get("symbols") or {}


def save_hist_all(symbols: dict, updated: str | None = None) -> None:
    _write_json(HIST_FILE, {
        "updated": updated or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols})


# ---------------------------------------------------------------- 拉取

def _kline_url(sym: str, count: int) -> str:
    s = _safe_sym(sym)
    if not 1 <= int(count) <= 800:
        raise ValueError(f"非法 bar 数: {count}")
    return (f"https://{KLINE_HOST}/appstock/app/usfqkline/get"
            f"?param={s},day,,,{int(count)},qfq")


def fetch_kline(sym: str, count: int = KLINE_COUNT_BACKFILL) -> list[list]:
    """腾讯美股日线（前复权）。返回 [[date,open,close,high,low,volume],...]，
    已剔除未收盘的尾 bar（美国东部 16:15 前视为未收盘）。"""
    def _get():
        r = _safe_get(_kline_url(sym, count))
        r.raise_for_status()
        return r.json()
    d = retry(_get, tries=2, delay=2.0)
    s = _safe_sym(sym)
    node = (d or {}).get("data", {}).get(s) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for row in rows:
        try:
            date = str(row[0])
            o, c = float(row[1]), float(row[2])
            h, l = float(row[3]), float(row[4])
            v = float(row[5]) if len(row) > 5 and row[5] else 0.0
        except (ValueError, IndexError, TypeError):
            continue
        if not c or c <= 0:
            continue
        out.append([date, o, c, h, l, v])
    out = [r for r in out if _bar_closed(r[0])]
    seen, dedup = set(), []
    for r in out:
        if r[0] not in seen:
            seen.add(r[0])
            dedup.append(r)
    return dedup


_SINA_JS_LOCK = threading.Lock()
_SINA_JS = None


def _sina_js_decode(payload: str):
    """新浪静态页 payload → item 列表（zh_js_decode 的 V8 求值）。

    py-mini-racer 的 V8 实例**不是线程安全的**：并发 new MiniRacer()/eval 会直接
    触发 native 崩溃（access violation，Python 层 try/except 抓不住、整进程死）。
    故用全局单实例 + 互斥锁串行化：HTTP 抓取照旧并发，只有毫秒级解码排队。
    （2026-09-10 修复：fetch_global._add_windows_us 的三线程池曾把整轮抓取崩死。）
    """
    global _SINA_JS
    from py_mini_racer import MiniRacer
    from akshare.stock.stock_us_sina import zh_js_decode
    with _SINA_JS_LOCK:
        if _SINA_JS is None:
            _SINA_JS = MiniRacer()
            _SINA_JS.eval(zh_js_decode)
        return _SINA_JS.call("d", payload) or []


def fetch_sina_us_daily(sym_plain: str) -> list[dict]:
    """新浪美股静态页日线原始解码（板块 ETF / 个股通用）。

    与 fetch_global._us_daily_closes 同一 URL 模板 + 同一解码路径
    （akshare zh_js_decode + py_mini_racer），2026-09-10 收为本模块导出的共享
    函数（fetch_global 导入使用，不再各留一份拷贝）。返回解码出的原始 item 列表
    [{date, open, close, high, low, volume}, ...]，不做收盘裁剪/去重——口径由
    调用方自定（us_market 剔未收盘 bar，fetch_global 只取收盘序列）。
    """
    s = str(sym_plain)
    if not _SINA_SYM_RE.match(s):
        raise ValueError(f"非法新浪符号: {sym_plain!r}")
    url = f"https://{SINA_HOST}/staticdata/us/{s}"

    def _get():
        r = _safe_get(url, timeout=20)
        r.raise_for_status()
        return r.text
    text = retry(_get, tries=2, delay=2.0)
    payload = text.split("=", 1)[1].split(";")[0].replace('"', "")
    return _sina_js_decode(payload)


def fetch_kline_sina(sym_plain: str) -> list[list]:
    """新浪美股静态页日线（板块 ETF 用，sym 如 'xlk'）。解码共享自
    fetch_sina_us_daily；返回 [[date,o,c,h,l,v],...]，已剔未收盘 bar 并去重。"""
    items = fetch_sina_us_daily(sym_plain)
    out = []
    for it in items:
        try:
            date = str(it.get("date"))[:10]
            c = float(it.get("close") or 0)
        except (ValueError, TypeError):
            continue
        if not c or c <= 0:
            continue
        o = float(it.get("open") or c)
        h = float(it.get("high") or c)
        l = float(it.get("low") or c)
        v = float(it.get("volume") or 0)
        out.append([date, o, c, h, l, v])
    out = [r for r in out if _bar_closed(r[0])]
    seen, dedup = set(), []
    for r in out:
        if r[0] not in seen:
            seen.add(r[0])
            dedup.append(r)
    return dedup


def _bar_closed(date_str: str) -> bool:
    """美股 bar 是否已收盘：date 当日美东 16:15 后视为收盘。"""
    try:
        from zoneinfo import ZoneInfo
        d = _dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        close_dt = _dt.datetime(d.year, d.month, d.day, 16, 15,
                                tzinfo=ZoneInfo("America/New_York"))
        now = _dt.datetime.now(ZoneInfo("America/New_York"))
        return now >= close_dt
    except Exception:  # noqa: BLE001 - 时区不可用时保守保留
        return True


def update_symbol(symbols: dict, sym: str, name: str, kind: str,
                  backfill_start: str | None) -> dict:
    """增量更新 symbols 内单符号历史；返回统计。本地为空或显式回补 → 全量。

    kind=sector（板块 ETF）走新浪静态页全量；其余走腾讯端点（增量 30 根）。"""
    s = _safe_sym(sym)
    old = symbols.get(s) or {}
    old_rows = old.get("rows") or []
    if s in _SINA_SOURCE_SYMS or kind == "sector":
        new_rows = fetch_kline_sina(s[2:].lower())   # usXLK -> xlk
    else:
        if backfill_start or not old_rows:
            count = KLINE_COUNT_BACKFILL
        else:
            count = 30
            newest = old_rows[-1][0]
            # 本地数据落后太久（>45 自然日）退回全量
            try:
                age = (_dt.date.today() - _dt.date.fromisoformat(newest)).days
                if age > 45:
                    count = KLINE_COUNT_BACKFILL
            except ValueError:
                count = KLINE_COUNT_BACKFILL
        new_rows = fetch_kline(sym, count)
    merged = {r[0]: r for r in old_rows}
    for r in new_rows:
        merged[r[0]] = r
    rows = [merged[k] for k in sorted(merged)]
    symbols[s] = {"name": name, "kind": kind, "rows": rows}
    return {"sym": s, "kind": kind, "n": len(rows),
            "first": rows[0][0] if rows else None,
            "last": rows[-1][0] if rows else None}


# ---------------------------------------------------------------- A 股日历

def load_ashare_dates() -> list[str]:
    """A 股交易日（ISO 日期升序）。缓存 7 天；来源 akshare 新浪交易日历
    （与 fetch_daily 的非交易日判定同源）。"""
    cached = _read_json(CAL_FILE)
    if cached and cached.get("dates"):
        try:
            fetched = _dt.date.fromisoformat(str(cached.get("fetched"))[:10])
            if (_dt.date.today() - fetched).days < CAL_TTL_DAYS:
                return list(cached["dates"])
        except ValueError:
            pass
    import akshare as ak
    df = retry(lambda: ak.tool_trade_date_hist_sina(), tries=2, delay=2.0)
    # trade_date 列元素可能是 datetime.date 或 pandas.Timestamp，统一取前 10 位 ISO
    dates = sorted({str(ts)[:10] for ts in df["trade_date"]})
    _write_json(CAL_FILE, {
        "fetched": _dt.datetime.now().isoformat(timespec="seconds"),
        "dates": dates})
    return dates


# ---------------------------------------------------------------- 因子构建

def _close_series(entry: dict | None) -> tuple[list[str], list[float]]:
    rows = (entry or {}).get("rows") or []
    return [r[0] for r in rows], [float(r[2]) for r in rows]


def _pct_at(dates_idx: dict, closes: list[float], u: str | None, n: int = 1) -> float | None:
    """U 日相对前 n 根 bar 的涨跌幅（%）。U 为 None 或无前 bar → None。

    dates_idx 为 date→下标 dict（每符号序列建一次）：旧实现 dates.index(u) 每次
    O(n)，build_factors 每晚全量重算时是主要常数开销。历史行不变、只追加新日期，
    增量追加 factors 理论可行，但要处理 us_date 对齐随 IXIC 日历补数而整体位移
    （T 行的 u 会变），收益/风险比不划算——保持全量重算，只做本处 O(1) 优化。
    """
    if u is None:
        return None
    i = dates_idx.get(u)
    if i is None or i < n or closes[i - n] <= 0:
        return None
    return (closes[i] / closes[i - n] - 1) * 100.0


def _mean(vals: list) -> float | None:
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def _round(v, nd=2):
    return None if v is None else round(v, nd)


def build_factors() -> dict:
    """从本地 hist_all 重建 factors.json（纯本地计算，不发请求）。

    对齐规则：C7 新时序（复盘在 T+1 凌晨美股收盘后完成）下，A 股 T 日快照的
    备选池实际生成于 T+1 04:30——此刻美股交易日 U=T 的场次已收盘（北京时间
    T+1 04:00/05:00），故 U = 最大的「≤ T」的美股交易日（以 IXIC 日历为准）。
    引擎/线上在 T+1 09:15 前消费，无未来函数；T 日 17:05 的过渡池拿到的是
    U=T-1（美股 T 尚未收盘，诚实降级），T+1 凌晨链会用完整场次覆盖。
    """
    symbols = load_hist_all()
    us_dates, _ = _close_series(symbols.get("usIXIC"))
    if not us_dates:
        raise RuntimeError("usIXIC 历史为空，先执行 --backfill")

    series_cache = {}

    def pct(sym, u, n=1):
        ent = series_cache.get(sym)
        if ent is None:
            ds, cs = _close_series(symbols.get(sym))
            ent = (cs, {d: i for i, d in enumerate(ds)})   # date→index 一次建好
            series_cache[sym] = ent
        cs, idx = ent
        return _round(_pct_at(idx, cs, u, n))

    today = _dt.date.today().isoformat()
    ashare = [d for d in load_ashare_dates() if FACTOR_START <= d <= today]
    rows = {}
    for t in ashare:
        u = next((d for d in reversed(us_dates) if d <= t), None)
        if u is None:
            continue
        bench = {}
        for sym, _key, name in US_BENCH:
            bench[name] = pct(sym, u)
            bench[name + "5"] = pct(sym, u, 5)
        sectors = {key: pct(sym, u) for sym, key, _n in US_SECTORS}
        sec_vals = [v for v in sectors.values() if v is not None]
        sec_up = _round(sum(1 for v in sec_vals if v > 0) / len(sec_vals), 3) if sec_vals else None
        proxy = {grp: _round(_mean([pct(s, u) for s in syms]))
                 for grp, syms in US_PROXY_SYMS.items()}
        rows[t] = {"us_date": u, **bench, "sectors": sectors,
                   "sec_up": sec_up, "proxy": proxy}

    payload = {
        "version": 1,
        "updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "align": "A股T → 最近一个在T+1日09:15(北京)前收盘的美股交易日U=最大≤T（us_date）",
        "rows": rows,
    }
    _write_json(FACTOR_FILE, payload)
    return payload


def load_factors() -> dict:
    """读取因子（供 speculate.py / strategy-iter 消费）；缺失返回 {}。"""
    return _read_json(FACTOR_FILE) or {}


def _session_closed(t_iso: str, now_et: _dt.datetime | None = None) -> bool:
    """美股 t_iso 场次是否已收盘：美东 16:15 后视为收盘——与 fetch_kline._bar_closed
    同一规则，保证「守卫判新鲜 ⟺ 此刻重新抓取必能拿到 T 场次 bar」。

    旧版用「factors.updated ≥ T+1 03:00」墙钟判定：收盘是北京 04:00（夏令）/05:00
    （冬令），03:00–05:15 之间的重建（手动补跑/链早触发）会把未收盘场次误判为终版。
    时区不可用时保守按未收盘（与 _bar_closed 的保守保留方向相反：守卫宁可拦）。"""
    try:
        from zoneinfo import ZoneInfo
        d = _dt.datetime.strptime(t_iso, "%Y-%m-%d").date()
        tz = ZoneInfo("America/New_York")
        close_dt = _dt.datetime(d.year, d.month, d.day, 16, 15, tzinfo=tz)
        now = now_et or _dt.datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)
        return now >= close_dt
    except Exception:  # noqa: BLE001 - 时区不可用时保守视为未收盘
        return False


def row_freshness(t_iso: str) -> tuple[dict | None, bool, str]:
    """T 日备选池的隔夜美股行是否已含完整场次（终版池生成前置守卫，2026-09-10）。

    终版口径：us_date == T（T 日美股场次已收盘且进入本地历史；抓取层 _bar_closed
    保证入库 bar 均为已收盘场次）。us_date < T 仅当此刻已过 T 场次收盘（美东
    16:15 = 北京 04:15 夏令 / 05:15 冬令，_session_closed 判定）才视为新鲜——覆盖
    美股节假日（T 无场次，链照常在 T+1 凌晨重建并落到最近场次）；否则一律视为
    隔夜未定（17:05 过渡池、链早触发等）。返回 (row, fresh, reason)；
    reason 仅在 fresh=False 时有意义。"""
    try:
        payload = load_factors() or {}
        row = (payload.get("rows") or {}).get(t_iso)
    except Exception:  # noqa: BLE001 - 读取异常按未定处理
        return None, False, "隔夜美股因子暂不可用"
    if row is None:
        return None, False, "隔夜美股因子缺失"
    u = str(row.get("us_date") or "")
    if u == t_iso:
        return row, True, ""
    if u and u < t_iso and _session_closed(t_iso):
        return row, True, ""
    return row, False, f"隔夜美股场次未定（当前 us_date={u or '无'}，待 T 日场次收盘）"


def _all_syms() -> list[str]:
    out = [s for s, _k, _n in US_BENCH] + [s for s, _k, _n in US_SECTORS]
    for syms in US_PROXY_SYMS.values():
        out.extend(syms)
    return sorted(set(out))


# ---------------------------------------------------------------- CLI

def _job_list() -> list[tuple[str, str, str]]:
    jobs: dict[str, tuple[str, str]] = {}
    for s, _k, n in US_BENCH:
        jobs[s] = (n, "bench")
    for s, _k, n in US_SECTORS:
        jobs[s] = (n, "sector")
    for syms in US_PROXY_SYMS.values():
        for s in syms:
            if s not in jobs:
                jobs[s] = (s[2:].split(".")[0], "stock")
    return [(s, n, k) for s, (n, k) in sorted(jobs.items())]


def cmd_run(backfill_start: str | None):
    symbols = load_hist_all()
    jobs = _job_list()
    ok = fail = 0
    for i, (sym, name, kind) in enumerate(jobs):
        try:
            st = update_symbol(symbols, sym, name, kind, backfill_start)
            ok += 1
            print(f"[{i+1}/{len(jobs)}] {sym:<12} {kind:<7} n={st['n']:<4} "
                  f"{st['first']}..{st['last']}")
        except Exception as e:  # noqa: BLE001 - 单符号失败不连坐，因子侧诚实缺失
            fail += 1
            print(f"[{i+1}/{len(jobs)}] {sym:<12} FAIL {type(e).__name__}: {str(e)[:100]}",
                  file=sys.stderr)
        # 每 10 个符号落一次盘（45+ 符号每晚逐个整库重写 hist_all.json 纯属浪费）；
        # 中断最多丢最近 9 个符号的增量，重跑即续上
        if (i + 1) % 10 == 0:
            save_hist_all(symbols)
        time.sleep(REQUEST_INTERVAL)
    save_hist_all(symbols)   # 循环结束统一收尾保存（含不足 10 个的尾段）
    print(f"fetch done: ok={ok} fail={fail}")
    if ok == 0:
        raise SystemExit("全部符号抓取失败，不重建因子（保留旧 factors.json）")
    payload = build_factors()
    ks = sorted(payload["rows"])
    print(f"factors rebuilt: {len(ks)} days {ks[0]}..{ks[-1]}")


def cmd_factors_only():
    payload = build_factors()
    ks = sorted(payload["rows"])
    print(f"factors rebuilt: {len(ks)} days {ks[0]}..{ks[-1]}")


def cmd_status():
    symbols = load_hist_all()
    for sym in _all_syms():
        e = symbols.get(sym) or {}
        rows = e.get("rows") or []
        print(f"{sym:<12} n={len(rows):<5} last={rows[-1][0] if rows else '-'}")
    fac = load_factors()
    rows = fac.get("rows") or {}
    ks = sorted(rows)
    print(f"factors.json   n={len(ks)} {ks[0] if ks else '-'}..{ks[-1] if ks else '-'} "
          f"updated={fac.get('updated')}")


def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="美股隔夜数据层（腾讯日线 + A股对齐因子）")
    ap.add_argument("--backfill", metavar="YYYYMMDD",
                    help="全量回补起点（缺省走增量）")
    ap.add_argument("--factors-only", action="store_true", help="只重建因子，不发请求")
    ap.add_argument("--status", action="store_true", help="查看各符号覆盖状态")
    args = ap.parse_args()
    if args.status:
        cmd_status()
    elif args.factors_only:
        cmd_factors_only()
    else:
        cmd_run(args.backfill)


if __name__ == "__main__":
    main()
