# -*- coding: utf-8 -*-
"""实时竞价板块 · 配置与观察池（单一来源）。

观察池 = data/auction/watchlist.txt（用户自选，永远包含，# 开头为注释行）
       + 最近复盘快照自动补充（涨停池 + 热股榜 TopN），去重、封顶。

竞价接口契约（hithink-finance market.auction-snapshot，schema 实测 2026-08-31）：
    --thscodes  逗号分隔完整 thscode（如 600519.SH），单次 ≤100 个 raw token
    --stage     live | final（盘后 live 自动回落 final 数据）
返回行字段：thscode/ticker/name/auction_price/auction_pct/auction_volume/
    auction_amount/auction_unmatched/auction_turnover_pct/
    auction_yesterday_ratio_pct/pre_close_price/open_price/last_price/float_market_cap
    ⚠ auction_volume_ratio 实测只在 09:15 首轮（data_status=not_ready 残留数据）偶发返回，
    live 正常轮次与 final 均不返回；展示层「量比昨」统一用 auction_yesterday_ratio_pct/100（2026-09-02 实查修正，
    此前前端误读 volume_ratio 导致强势候选永远 0 命中）。

本模块产出文件契约（前端与 server 只读不写）：
    industry_map.json {built_at, date8, industries, stocks, map: {thscode: 一级行业}}
                   由 auc_industry 扫 90 个一级行业成分维护；build_watchlist 落盘时用它
                   回填缺失行业，server 展示层再对 items 老池代码内存兑底（不回写）。
    watchmap.json  {thscode: {i: 行业, n: 名称, s: 来源}}
                   s 为 U（自选）/L（上一交易日涨停池）/H（热股榜）的可叠加串（如 "UL"）。
                   接力类展示（涨停股今日溢价 vs 池内其余）完全依赖这个字段，
                   没标就会把整个观察池当成涨停股算，结论错。
    rounds_meta.json {date, rounds: [{ts,label,stage,count,codes_total,in_window,errors,phase,data_status}]}
                   与 series.json 同序同过滤，但不含 items：体检面板画逐轮覆盖条不该
                   为此读几百 KB 大文件。由 auc_collector.append_series 统一写。
"""
import json
import os
import re
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))

from thscodes import to_thscode  # noqa: E402  代码转换单一来源（backend/thscodes.py）

DATA_DIR = os.path.join(PROJECT_ROOT, "data", "auction")
WATCHLIST_TXT = os.path.join(DATA_DIR, "watchlist.txt")
STATUS_JSON = os.path.join(PROJECT_ROOT, ".status", "auction.json")
LOCK_PATH = os.path.join(PROJECT_ROOT, ".status", "fetch-auction.lock")

# 时间线（本地时钟，Asia/Shanghai）
LIVE_START = (9, 15, 0)      # live 轮询窗口起点（含）
LIVE_END = (9, 24, 59)       # live 轮询窗口终点（含）
FINAL_AT = (9, 25, 10)       # 终态抓取时刻
INTERVAL = 30                # live 轮询周期（秒）
BATCH = 90                   # 单批 thscodes（契约上限 100，留余量）
BATCH_GAP = 3                # 批间隔（秒）
MAX_CODES = 300              # 观察池封顶
HOT_TOP = 60                 # 热股榜取前 N
SERIES_KEEP_ROUNDS = 60      # series.json 最多保留轮数
MISS_GRACE = 25              # 轮次错过宽限（秒）：超时太多的时点直接跳过

_CODE_KEYS = ("代码", "code", "股票代码", "thscode", "ticker")

# to_thscode 本体在 backend/thscodes.py（顶部 import 进来）：曾与 speculate.thscode_of、
# fetch_global._thscode 三份实现且规则不一致，2026-09-04 收敛为单一来源。


def row_code(row):
    """复盘快照行代码字段容错读取（中英键名都试）。"""
    for k in _CODE_KEYS:
        v = (row or {}).get(k)
        if v:
            return to_thscode(v)
    return None


def parse_watchlist(raw):
    """解析观察池原文 → (thscode 列表, 认不出的 token 列表)。

    必须同时返回无效项：否则用户写了「茅台」或「6005」这类错代码会被静默丢弃，
    只会看到「已保存 2 个」而不知道少了一个。"""
    out, bad = [], []
    for line in (raw or "").splitlines():
        line = line.split("#", 1)[0]
        for tok in re.split(r"[,\s;、]+", line):
            if not tok:
                continue
            tc = to_thscode(tok)
            if tc:
                if tc not in out:
                    out.append(tc)
            else:
                bad.append(tok)
    return out, bad


def read_watchlist_text():
    """自选文件原文（给前端编辑框回填用）。

    必须用原文而不是归一化后的代码列表：否则用户在页面上保存一次
    就会把文件里的注释与示例行全冲掉。"""
    try:
        with open(WATCHLIST_TXT, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read_watchlist():
    """自选文件 → thscode 列表（保持顺序、去重；# 注释忽略）。"""
    return parse_watchlist(read_watchlist_text())[0]


def write_watchlist(raw_text):
    """原子写自选文件，返回归一化后的 thscode 列表。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = WATCHLIST_TXT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write((raw_text or "").strip() + "\n")
    os.replace(tmp, WATCHLIST_TXT)
    return read_watchlist()


def load_json(name):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_IMAP_CACHE = {"key": None, "map": {}}


def industry_map():
    """个股→一级行业全量映射（auc_industry 维护），按文件 mtime 缓存。

    10s 轮询反复读 160KB JSON 没必要；文件没动直接返内存旧副本。
    """
    try:
        key = os.path.getmtime(os.path.join(DATA_DIR, "industry_map.json"))
    except OSError:
        return {}
    if _IMAP_CACHE["key"] != key:
        d = load_json("industry_map.json") or {}
        _IMAP_CACHE.update(key=key, map=d.get("map") or {})
    return _IMAP_CACHE["map"]


def watchmap_fill_industry(wm, codes):
    """展示层行业兑底：items 出现过但 watchmap 缺行业的代码用全量映射补上。

    池子换代是常态（今日热榜 items 是早池 108 只，收盘后 watchmap 已重建为
    明日池 103 只），不兑底会把过渡期打成一片「未分类」。只改内存副本供
    展示，不回写 watchmap.json——池子口径（来源 s/接力判定）必须保持纯净。
    """
    imap = industry_map()
    if not imap:
        return wm
    for tc in codes:
        if not tc:
            continue
        e = wm.get(tc)
        if e is None or not e.get("i"):
            ind = imap.get(tc)
            if ind:
                ee = dict(e or {"i": "", "n": "", "s": ""})
                ee["i"] = ind
                wm[tc] = ee
    return wm


def save_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, p)
    return p


def save_status(obj):
    """任务状态 → .status/auction.json（/api/health 展示用，失败不影响采集）。"""
    try:
        os.makedirs(os.path.dirname(STATUS_JSON), exist_ok=True)
        tmp = STATUS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, STATUS_JSON)
    except Exception as e:  # noqa: BLE001
        print(f"状态写入失败（忽略）: {e}", file=sys.stderr)


def auto_pool_rows():
    """从最近复盘快照取补充池候选行：[(thscode, 行业, 名称, 来源)]。

    涨停池（含行业标注）优先，其次热股榜 TopN；同时供 watchmap 行业映射用。
    来源标必须带：前端要靠它算「上一交易日涨停股今日竞价溢价（情绪接力）」，
    没标就只能把整个观察池当涨停股看，结论会错。L=涨停池，H=热股榜。
    """
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend", "recap"))
    import snapio
    rows = []
    dates = snapio.list_dates()
    if not dates:
        return rows
    for d in dates[:3]:          # 最多回看 3 个快照找涨停池
        snap = snapio.load(d) or {}
        data = ((snap.get("modules") or {}).get("limit_up_pool") or {}).get("data") or []
        got = [(c, (r.get("所属行业") or r.get("行业") or ""),
                (r.get("名称") or r.get("name") or ""), "L")
               for r in data if (c := row_code(r))]
        rows += got
        if got:
            break
    for d in dates[:2]:          # 热股榜取最近有数据的快照
        snap = snapio.load(d) or {}
        data = ((snap.get("modules") or {}).get("hot_stock") or {}).get("data") or []
        got = [(c, (r.get("所属行业") or r.get("行业") or ""),
                (r.get("名称") or r.get("name") or ""), "H")
               for r in data[:HOT_TOP] if (c := row_code(r))]
        rows += got
        if got:
            break
    return rows


def build_watchlist():
    """合并自选与自动池：自选在前，去重封顶；同时落盘 watchmap.json。

    watchmap 结构 {thscode: {i: 行业, n: 名称, s: 来源}}，s 由 U（自选）/L（涨停池）/H（热股榜）
    拼接，一只股可同时命中多个来源（如 "UL"）。自选里手写但不在自动池的代码也要进 watchmap，
    否则前端分不清「自选但无行业标注」和「根本不认识这只」。

    行业口径（两层）：优先用池子行自带标注（涨停池），缺失的从 industry_map.json
    （同花顺一级行业成分反查全市场映射，auc_industry 维护）回填；两层都拿不到
    才归「未分类」——宁缺勿错，不拿名字猜行业硬凑。
    """
    user = read_watchlist()
    watchmap = {}
    for tc, ind, nm, src in auto_pool_rows():
        e = watchmap.setdefault(tc, {"i": ind, "n": nm, "s": ""})
        if src not in e["s"]:
            e["s"] += src
    for tc in user:
        e = watchmap.setdefault(tc, {"i": "", "n": "", "s": ""})
        if "U" not in e["s"]:
            e["s"] = "U" + e["s"]
    # 行业回填：纯热股榜来源没标注，用全量映射补上
    imap = industry_map()
    if imap:
        for tc, e in watchmap.items():
            if not e.get("i") and tc in imap:
                e["i"] = imap[tc]
    merged = list(user)
    for tc in watchmap:
        if tc not in merged:
            merged.append(tc)
    merged = merged[:MAX_CODES]
    auto_only = [tc for tc, e in watchmap.items() if e["s"] and "U" not in e["s"]]
    meta = {"user": len(user), "auto": len(auto_only), "total": len(merged),
            "src_limit": sum(1 for e in watchmap.values() if "L" in e["s"]),
            "src_hot": sum(1 for e in watchmap.values() if "H" in e["s"]),
            "src_watch": len(user)}
    if merged:
        save_json("watchmap.json", watchmap)
    return merged, meta
