# -*- coding: utf-8 -*-
"""
A股看板 · 统一服务（竞价 + 轮动 + 复盘 + 全球 + 量化 五板块，单端口）

用法:
    python server.py            # 默认 127.0.0.1:8000（可用环境变量 AK_PORT 改）
    python server.py --port 8080

路由（板块排列顺序与顶栏导航一致：竞价 → 轮动 → 复盘 → 全球 → 量化）:
    静态:
        /auction, /auction/   实时竞价（web/auction/index.html）
        /                     日内轮动仪表盘（web/index.html，根路径入口不变）
        /recap, /recap/       盘后复盘看板（web/recap/index.html，同源内嵌）
        /global, /global/     全球总览（web/global/index.html）
        /quant, /quant/       量化平台工作台（web/quant/index.html，内置 quant/ 引擎）
        /lib/...              共享 JS/CSS/地图（ETag + 304；echarts/world.json 长缓存）
    实时竞价 API（只读 data/auction/ 采集产出；异动回算在 backend/auction/auc_alerts.py）:
        /api/auction          竞价面板（live/final/benchmark/状态/观察池/rounds_meta/时间线配置，ETag/304）
        /api/auction/series   当日全部轮次时序（单股竞价曲线）
        /api/auction/status   采集状态与观察池（轻量，页面补抓期间轮询用）
        POST /api/auction/watchlist     更新自选观察池（归一化为完整 thscode）
        POST /api/fetch-auction         后台补抓（文件锁防重入，日志 .status/logs/）
    轮动数据 API:
        /api/dates            轮动日期列表（YYYY-MM-DD）
        /api/day?date=        当日板块分时（同花顺一级行业 90 个，盘中逐分钟快照轮询累积；白名单校验，防路径穿越）
        /api/boards           板块清单（同花顺一级行业，src=ths，与竞价页/复盘页同一口径）
        /api/rotation-stats   轮动统计（读 data/rotation/panel/stats.json，派生层产出）
        /api/rotation-matrix  板块强度矩阵 + 日内形态（读 panel/matrix.json）
    复盘数据 API:
        /api/recap/dates      复盘快照日期（兼容 .json / .json.gz）
        /api/recap/modules    模块契约 registry（单一来源 backend/recap/modules.py）
        /data/YYYYMMDD.json   复盘快照（白名单：仅 8 位数字）
        GET/POST /api/recap/note?date=YYYYMMDD  复盘笔记（存 data/recap/notes/，localStorage 仅兜底）
         /api/sentiment       情绪指数序列（recap/panel/sentiment.csv 派生层产出）
         /api/board-members   板块近5日走势+池内个股（聚合逻辑在 backend/board_members.py）
         /api/industry-map    个股→同花顺一级行业映射（data/auction/industry_map.json 单一来源，全站共用）
    全球总览 API:
        /api/global           全球总览数据（读 data/global/global.json，fetch_global.py 产出）
        POST /api/fetch-global 后台重抓全球数据（文件锁防重入）
    量化平台 API（引擎已内置于 quant/，后端接线见 backend/quant/quant_api.py）:
        /api/quant            总览面板（collector 聚合回测流水/报告/研究/策略库，15s 缓存）
        /api/quant/meta       前端表单单一事实源（策略族参数/规则 DSL/选股条件/本地标的清单）
        POST /api/quant/backtest   同步回测（内置族/规则 spec，返回指标+净值曲线+成交表）
        GET/POST /api/quant/strategies[|/get|/delete]  策略库读写
        POST /api/quant/job   提交长任务 screener|signals|grid；GET /api/quant/job/<id> 轮询
    运维:
        /api/health           数据管家（新鲜度/失败明细/锁状态）
        POST /api/fetch / /api/fetch-rotation   后台抓取（文件锁防重入，日志 .status/logs/）

写接口安全（2026-09-04 加固，原为局域网零鉴权）:
    - 所有 POST 过写接口闸门：浏览器跨站 POST 的 Origin/Referer 与本服务 Host 不同源 → 403
      （拦 CSRF：恶意网页借用户浏览器打内网接口）；看板页面同源访问与手机 Tailscale 访问不受影响。
    - 非本机来源的无 Origin 客户端（curl/脚本）：配置环境变量 AK_WRITE_TOKEN 后必须携带
      X-AK-Token 头（或 ?token=）才能写；未配置时行为同旧版（放行），启动横幅会提示如何开启。

设计边界：
- 全站板块口径统一为同花顺一级行业指数（881xxx，90 个，2026-09-01 起）：轮动采集 backend/rotation/ths_collect.py、竞价板块强度 sector.json、复盘行业模块与钻取、个股行业归属 industry_map.json 全部同一目录同一名单；东财口径仅存归档 daily_legacy_eastmoney/ 与 legacy 脚本，不再接入任何链路与文案。
- 启动落点（先打开哪个板块）由 backend/landing.py 统一判定：工作日 09:10–09:30 落实时竞价，其余落日内轮动；server.py 与 start.py 共用，不在两处各写一套时间判断。
- HTTP 层不做业务计算：情绪指数/轮动统计/强度矩阵全部由 backend/derive.py 产出面板文件；异动回算在 backend/auction/auc_alerts.py、板块钻取在 backend/board_members.py、行情代理与单代码缓存在 backend/quote_service.py（2026-09-04 迁出），本文件只做路由/缓存/序列化。
- 快照读写统一走 backend/recap/snapio（自动兼容 gzip 归档）。
- 静态服务仅暴露 web/ 下的五个页面目录与 lib/（主站 + recap/global/quant/auction）；data 路由白名单校验，杜绝目录浏览与穿越。
"""
import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, HTTPServer

os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))
sys.path.insert(0, os.path.join(ROOT, "backend", "quant"))
sys.path.insert(0, os.path.join(ROOT, "backend", "auction"))

import derive          # noqa: E402  派生层（面板文件产出方）
import lockutil        # noqa: E402
import snapio          # noqa: E402
import collector as quant_collector  # noqa: E402  量化平台只读采集层（backend/quant）
import quant_api                     # noqa: E402  量化引擎接线 API（回测/选股/信号/研究）
import auc_config                    # noqa: E402  竞价板块配置（观察池/自选单一来源）
import auc_alerts                    # noqa: E402  竞价全天异动回算（2026-09-04 迁出 HTTP 层）
import pool_exec                     # noqa: E402  昨日备选池×今日竞价执行判定（2026-09-04）
import board_members                 # noqa: E402  板块钻取聚合（2026-09-04 迁出 HTTP 层）
import quote_service                 # noqa: E402  行情代理 + 单代码缓存（2026-09-04 迁出）
from landing import describe as landing_describe   # noqa: E402  启动落点口径（与 start.py 共用）
from landing import landing_path                   # noqa: E402
from modules import MODULE_REGISTRY  # noqa: E402
import auc_industry                  # noqa: E402  个股→一级行业映射（与竞价页同一份文件）

MAIN_WEB = os.path.join(ROOT, "web")
RECAP_WEB = os.path.join(ROOT, "web", "recap")
GLOBAL_WEB = os.path.join(ROOT, "web", "global")
QUANT_WEB = os.path.join(ROOT, "web", "quant")
AUCTION_WEB = os.path.join(ROOT, "web", "auction")
LIB_WEB = os.path.join(MAIN_WEB, "lib")
AUCTION_FETCH_SCRIPT = os.path.join(ROOT, "backend", "auction", "auc_collector.py")
AUCTION_FETCH_CWD = os.path.join(ROOT, "backend", "auction")
GLOBAL_JSON = os.path.join(ROOT, "data", "global", "global.json")
ROT_DAILY = os.path.join(ROOT, "data", "rotation", "daily")
ROT_RAW = os.path.join(ROOT, "data", "rotation", "intraday")
ROT_PANEL = os.path.join(ROOT, "data", "rotation", "panel")
ROT_BOARDS = os.path.join(ROOT, "data", "rotation", "boards.json")
RECAP_DATA = os.path.join(ROOT, "data", "recap")
RECAP_PANEL = os.path.join(RECAP_DATA, "panel")
RECAP_NOTES = os.path.join(RECAP_DATA, "notes")
FETCH_SCRIPT = os.path.join(ROOT, "backend", "recap", "fetch_daily.py")
FETCH_CWD = os.path.join(ROOT, "backend", "recap")
ROT_FETCH_SCRIPT = os.path.join(ROOT, "backend", "rotation", "ths_collect.py")
ROT_FETCH_CWD = os.path.join(ROOT, "backend", "rotation")
GLOBAL_FETCH_SCRIPT = os.path.join(ROOT, "backend", "global", "fetch_global.py")
GLOBAL_FETCH_CWD = os.path.join(ROOT, "backend", "global")
STATUS_DIR = os.path.join(ROOT, ".status")
LOG_DIR = os.path.join(STATUS_DIR, "logs")
LOCK_RECAP = os.path.join(STATUS_DIR, "fetch-recap.lock")
LOCK_ROT = os.path.join(STATUS_DIR, "fetch-rotation.lock")
LOCK_GLOBAL = os.path.join(STATUS_DIR, "fetch-global.lock")

WRITE_TOKEN_ENV = "AK_WRITE_TOKEN"   # 配置后非本机写请求必须携带 X-AK-Token

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE8_RE = re.compile(r"\d{8}")

# 服务为多线程（ThreadingMixIn）：所有进程内缓存一律配锁；重活（文件解析/子进程）
# 在锁外做，锁只护字典读写。
_CACHE_LOCK = threading.RLock()
_PROC_LOCK = threading.Lock()
_fetch_procs = {}  # key -> 正在运行的手动抓取进程（同进程防重入）
_auto_sector_ts = [0.0]  # sector 自愈补抓上次试探时刻（10 分钟节流；列表便于函数内改写）
_auto_rot_ts = [0.0]     # 轮动断流自愈上次试探时刻（同上，10 分钟节流）


# ---------------- 小工具 ----------------

def rot_dates():
    if not os.path.isdir(ROT_DAILY):
        return []
    return sorted(f[:-5] for f in os.listdir(ROT_DAILY)
                  if f.endswith(".json") and _DATE_RE.fullmatch(f[:-5]))


def recap_dates():
    return snapio.list_dates(RECAP_DATA)


def load_status(name):
    fp = os.path.join(STATUS_DIR, name)
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def file_fetched_at(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("fetched_at") or ""
    except Exception:
        return ""


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _hours_since(timestr):
    """距离某个时间字符串（%Y-%m-%d %H:%M:%S）过去了多少小时；无法解析返回 None。"""
    if not timestr:
        return None
    try:
        t = time.mktime(time.strptime(timestr, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None
    return round((time.time() - t) / 3600, 1)


def is_trading_time(now=None):
    now = now or time.localtime()
    if now.tm_wday >= 5:
        return False
    hm = now.tm_hour * 60 + now.tm_min
    return 9 * 60 + 15 <= hm <= 15 * 60 + 5


def rotation_stall():
    """盘中采集停滞判定（2026-09-02 静默失败事故后补）。

    工作日盘中，今日逐轮 raw 文件不存在或超过 5 分钟没有新数据点 →
    采集器没在跑（计划任务没启动/进程被杀）。此前页面只能显示「今日无数据」，
    无法区分「还没开盘」与「任务根本没跑」，错过一整天才被发现。
    交易日确认复用竞价状态文件（竞价采集器 09:14 已查过交易日历，不额外联网）：
    竞价状态标了 non-trade-day 说明今日休市，不出警告。
    """
    now = time.localtime()
    if now.tm_wday >= 5:
        return None
    hm = now.tm_hour * 60 + now.tm_min
    if not (9 * 60 + 35 <= hm <= 15 * 60 + 5):
        return None
    auc_st = load_status("auction.json") or {}
    if str(auc_st.get("date") or "") == time.strftime("%Y%m%d") \
            and "non-trade-day" in str(auc_st.get("note") or ""):
        return None
    today = time.strftime("%Y-%m-%d")
    p = os.path.join(ROT_RAW, today + ".raw.json")
    try:
        age_min = int((time.time() - os.path.getmtime(p)) / 60)
    except OSError:
        return {"stalled": True, "age_minutes": None,
                "note": "今日没有任何盘中数据点，采集任务可能从未启动"
                        "（检查 Windows 计划任务 rotation-intraday-fetch）"}
    if age_min > 5:
        return {"stalled": True, "age_minutes": age_min,
                "note": f"已 {age_min} 分钟无新盘中数据点，采集器可能已退出或被杀"
                        "（检查计划任务，或在新鲜度胶囊里手动重抓）"}
    return {"stalled": False, "age_minutes": age_min, "note": None}


# ---------------- 派生面板读取（带进程内缓存） ----------------

_panel_cache = {}


def _read_panel(path, parser):
    """文件 mtime 变化才重新解析（线程安全：锁只护字典，解析在锁外）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _CACHE_LOCK:
        ent = _panel_cache.get(path)
    if ent and ent[0] == mtime:
        return ent[1]
    try:
        obj = parser(path)
    except Exception:
        return ent[1] if ent else None
    with _CACHE_LOCK:
        _panel_cache[path] = (mtime, obj)
    return obj


def _parse_sentiment_csv(path):
    rows = []
    version = None
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            version = r.get("version") or version

            def _num(k, cast):
                v = r.get(k)
                if v in (None, ""):
                    return None
                try:
                    return cast(v)
                except ValueError:
                    return None
            rows.append({
                "date": r["date"],
                "index": _num("index", float),
                "label": r.get("label") or "",
                "zt": _num("zt", int) or 0,
                "dt": _num("dt", int) or 0,
                "max_lb": _num("max_lb", int) or 0,
                "promo_rate": _num("promo_rate", float),
                "up_ratio": _num("up_ratio", float),
                "zhaban": _num("zhaban", int) or 0,
            })
    return {"series": rows, "weights_version": int(version) if version else None}


def sentiment_payload():
    derive.ensure_fresh()
    p = os.path.join(RECAP_PANEL, "sentiment.csv")
    data = _read_panel(p, _parse_sentiment_csv)
    if data is None:
        # 面板缺失兜底：现算一次（首启或派生层故障时不至于白屏）
        try:
            sent, _ = derive.compute_sentiment()
            data = {"series": [
                {k: r[k] for k in ("date", "index", "label", "zt", "dt", "max_lb",
                                   "promo_rate", "up_ratio", "zhaban")} for r in sent],
                "weights_version": derive.WEIGHTS_VERSION}
        except Exception:
            data = {"series": [], "weights_version": None}
    return data


def rotation_stats_payload():
    derive.ensure_fresh()
    p = os.path.join(ROT_PANEL, "stats.json")
    data = _read_panel(p, lambda x: json.load(open(x, encoding="utf-8")))
    return data or {"dates": [], "speed": [], "top10": {}, "persistent": [],
                    "newcomers": [], "leaders_5d": []}


def rotation_matrix_payload():
    derive.ensure_fresh()
    p = os.path.join(ROT_PANEL, "matrix.json")
    return _read_panel(p, lambda x: json.load(open(x, encoding="utf-8"))) or {}


# ---------------- 量化平台（只读聚合，外部目录非本项目数据，不进 derive 面板体系） ----------------

_QUANT_TTL = 15.0          # 目录扫描结果缓存秒数
_quant_cache = {"ts": 0.0, "data": None}


def quant_payload(force=False):
    now = time.time()
    with _CACHE_LOCK:
        cached = _quant_cache["data"]
        if not force and cached is not None and now - _quant_cache["ts"] <= _QUANT_TTL:
            return json.loads(json.dumps(cached, ensure_ascii=False))
    try:
        data = quant_collector.collect()
        with _CACHE_LOCK:
            _quant_cache["data"] = data
            _quant_cache["ts"] = now
    except Exception as e:
        with _CACHE_LOCK:
            if _quant_cache["data"] is None:
                return {"error": f"量化平台采集失败：{e}"}
        data = _quant_cache["data"]
    return json.loads(json.dumps(data, ensure_ascii=False))  # 浅拷贝，不污染缓存


# ---------------- 行业映射（mtime 缓存） ----------------

_indmap_cache = {"mtime": 0.0, "payload": None}


def industry_map_payload():
    """个股→同花顺一级行业（裸 6 位代码键，按文件 mtime 缓存）。

    单一来源就是竞价采集器维护的 data/auction/industry_map.json，
    复盘页行业列/focus 链接与钻取归属共用这一份，不再各算各的。"""
    p = os.path.join(ROOT, "data", "auction", "industry_map.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = 0.0
    with _CACHE_LOCK:
        if _indmap_cache["payload"] is not None and _indmap_cache["mtime"] == mt:
            return _indmap_cache["payload"]
    raw = auc_industry.load_map()
    payload = {"src": "ths", "stocks": len(raw),
               "ticker6": {str(k).split(".")[0]: v for k, v in raw.items()}}
    with _CACHE_LOCK:
        _indmap_cache.update(mtime=mt, payload=payload)
    return payload


# ---------------- 运维健康聚合 ----------------

def health_payload():
    rd, cd = rot_dates(), recap_dates()
    rot_path = os.path.join(ROT_DAILY, rd[-1] + ".json") if rd else None
    cap_path = snapio.snap_path(RECAP_DATA, cd[0]) if cd else None
    rot_ft = file_fetched_at(rot_path) if rot_path else ""
    cap_ft = file_fetched_at(cap_path) if cap_path else ""
    rot_failed, rot_coverage = [], None
    if rot_path:
        d = _read_json_file(rot_path) or {}
        rot_failed = (d.get("failed") or [])[:10]
        rot_coverage = d.get("coverage")
    recap_errors = []
    if cap_path:
        try:
            d = snapio.load(cd[0]) or {}
            recap_errors = [
                {"module": k, "error": (v.get("error") or "")[:120]}
                for k, v in (d.get("modules") or {}).items()
                if v.get("status") == "error"]
        except Exception:
            pass
    glob_d = _read_json_file(GLOBAL_JSON) if os.path.exists(GLOBAL_JSON) else None
    glob_ft = (glob_d or {}).get("fetched_at") or ""
    glob_errors = list((glob_d or {}).get("errors") or {})
    with _PROC_LOCK:
        gp_proc = _fetch_procs.get("global")
    auc_st = load_status("auction.json")
    auc_live = auc_config.load_json("live.json") or {}
    auc_final = auc_config.load_json("final.json") or {}
    return {
        "service": "ak-dashboard",   # start.py 端口占用时校验对端身份用
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "is_trading_time": is_trading_time(),
        "rotation": {
            "last_date": rd[-1] if rd else None,
            "fetched_at": rot_ft,
            "age_hours": _hours_since(rot_ft),
            "failed": rot_failed,
            "coverage": rot_coverage,
            "task": load_status("rotation.json"),
            "stall": rotation_stall(),
            "fetching": lockutil.held_info(LOCK_ROT) is not None,
        },
        "recap": {
            "last_date": cd[0] if cd else None,
            "fetched_at": cap_ft,
            "age_hours": _hours_since(cap_ft),
            "errors": recap_errors,
            "task": load_status("recap.json"),
            "fetching": lockutil.held_info(LOCK_RECAP) is not None,
        },
        "global": {
            "last_date": (glob_d or {}).get("date"),
            "fetched_at": glob_ft,
            "age_hours": _hours_since(glob_ft),
            "errors": glob_errors,
            "fetching": (lockutil.held_info(LOCK_GLOBAL) is not None
                         or (gp_proc is not None and gp_proc.poll() is None)),
        },
        "auction": {
            "last_run": (auc_st or {}).get("last_run"),
            "date": (auc_st or {}).get("date"),
            "rounds": (auc_st or {}).get("rounds"),
            "live_updated_at": auc_live.get("updated_at"),
            "final_updated_at": auc_final.get("fetched_at"),
            "fetching": lockutil.held_info(auc_config.LOCK_PATH) is not None,
        },
    }


def auction_payload():
    """竞价面板聚合：live/final/benchmark/状态/观察池/逐轮元数据一次取齐（HTTP 层零计算）。

    注意：不放进时间戳类字段，否则 ETag 每次都变，304 轮询机制失效。
    rounds_meta 是采集器写的轻量逐轮元数据（不含 items），体检面板拿它画覆盖条，
    不必为了几个时间戳去读几百 KB 的 series.json。
    alerts 是 series.json 的 mtime 缓存回算（文件不变零开销），随轮次更新自然进 ETag。
    """
    def _hms(t):
        return "%02d:%02d:%02d" % t
    _live = auc_config.load_json("live.json")
    _final = auc_config.load_json("final.json")
    # 展示层行业兑底：池子换代过渡期，今日 items 里的老池代码在 watchmap 没条目，
    # 不补会把热榜打成一片「未分类」（细节/原则见 auc_config.watchmap_fill_industry）
    _wm = auc_config.watchmap_fill_industry(
        auc_config.load_json("watchmap.json") or {},
        [r.get("thscode") for rnd in (_live, _final) if rnd
         for r in ((rnd.get("round") or {}).get("items") or [])])
    return {
        "live": _live,
        "final": _final,
        "benchmark": auc_config.load_json("benchmark.json"),
        "status": load_status("auction.json"),
        "watchlist": auc_config.read_watchlist(),
        "watchlist_text": auc_config.read_watchlist_text(),   # 原文，编辑框回填用（不丢注释）
        "watchmap": _wm,
        "rounds_meta": auc_config.load_json("rounds_meta.json") or {"date": None, "rounds": []},
        "alerts": auc_alerts.payload(ROOT),   # 全天异动回算（口径/动机见 auc_alerts.py）
        "pool_exec": pool_exec.payload(),     # 昨日备选池×今日竞价执行判定（冻结执行层窗口）
        "sector": auc_config.load_json("sector.json") or {"date": None, "rows": []},
        "config": {"interval": auc_config.INTERVAL, "batch": auc_config.BATCH,
                   "max_codes": auc_config.MAX_CODES, "hot_top": auc_config.HOT_TOP,
                   # 体检面板要算「应有的轮次格子」，时间线参数得给前端（单一来源仍是 auc_config）
                   "live_start": _hms(auc_config.LIVE_START), "live_end": _hms(auc_config.LIVE_END),
                   "final_at": _hms(auc_config.FINAL_AT), "miss_grace": auc_config.MISS_GRACE},
        "fetching": lockutil.held_info(auc_config.LOCK_PATH) is not None,
    }


def auction_series_payload():
    data = auc_config.load_json("series.json")
    return data or {"date": None, "rounds": []}


def auction_brief():
    """启动横幅用的竞价数据摘要（只读产出文件，不做业务计算）。

    竞价接口是 today-only，所以「live 是今天的」优先于终态；
    两个数据文件都没有时不回退到任务状态日（那只是上次跑的时刻，不是数据日）。"""
    live = auc_config.load_json("live.json") or {}
    fin = auc_config.load_json("final.json") or {}
    st = load_status("auction.json") or {}
    d8 = str(fin.get("date") or "")
    rnd = fin.get("round") or {}
    ld = str(live.get("date") or "")
    if ld == time.strftime("%Y%m%d"):
        d8, rnd = ld, (live.get("round") or {})
    n = rnd.get("count") or 0
    if len(d8) != 8 or not d8.isdigit():
        return "（无，先跑 backend/auction/auc_collector.py --once 或注册 09:14 计划任务）"
    label = f"{d8[:4]}-{d8[4:6]}-{d8[6:]} {n} 只"
    if st.get("mode") == "manual":
        label += "（手动补抓）"
    return label


# ---------------- 路由表（2026-09-04：巨型 if 链 → (method, regex) → handler） ----------------

# 大体积、几乎不变的 vendored 资源长缓存（1MB echarts / 1MB world.json 每次刷新全量重拉
# 与「为手机省带宽」的 gzip 优化自相矛盾）；其余 lib 文件开发期常改，只给 ETag 协商缓存。
_LIB_IMMUTABLE = {"/lib/echarts.min.js", "/lib/map/world.json"}
_LIB_CTYPES = {".js": "text/javascript; charset=utf-8",
               ".css": "text/css; charset=utf-8",
               ".json": "application/json; charset=utf-8",
               ".svg": "image/svg+xml",
               ".png": "image/png",
               ".woff2": "font/woff2"}

GET_ROUTES = [
    (r"^/api/dates$", "api_dates"),
    (r"^/api/health$", "api_health"),
    (r"^/api/sentiment$", "api_sentiment"),
    (r"^/api/board-members$", "api_board_members"),
    (r"^/api/rotation-stats$", "api_rotation_stats"),
    (r"^/api/rotation-matrix$", "api_rotation_matrix"),
    (r"^/api/day$", "api_day"),
    (r"^/api/boards$", "api_boards"),
    (r"^/api/quote$", "api_quote"),
    (r"^/api/industry-map$", "api_industry_map"),
    (r"^/api/recap/dates$", "api_recap_dates"),
    (r"^/api/recap/modules$", "api_recap_modules"),
    (r"^/api/recap/note$", "api_recap_note"),
    (r"^/data/(?P<name>\d{8}\.json)$", "api_snapshot"),
    (r"^/api/global$", "api_global"),
    (r"^/api/quant$", "api_quant"),
    (r"^/api/quant/meta$", "api_quant_meta"),
    (r"^/api/quant/strategies$", "api_quant_strategies"),
    (r"^/api/quant/jobs$", "api_quant_jobs"),
    (r"^/api/quant/job/(?P<jid>[^/]+)$", "api_quant_job_get"),
    (r"^/api/auction$", "api_auction"),
    (r"^/api/auction/series$", "api_auction_series"),
    (r"^/api/auction/status$", "api_auction_status"),
    (r"^/lib/(?P<rest>.+)$", "api_lib_static"),
    (r"^/quant-results/(?P<sub>.+)$", "api_quant_results"),
    (r"^/recap(?:/|/index\.html)?$", "page_recap"),
    (r"^/global(?:/|/index\.html)?$", "page_global"),
    (r"^/auction(?:/|/index\.html)?$", "page_auction"),
    (r"^/quant(?:/|/index\.html)?$", "page_quant"),
]
GET_ROUTES = [(re.compile(p), fn) for p, fn in GET_ROUTES]

POST_ROUTES = [
    (r"^/api/fetch$", "post_fetch_recap"),
    (r"^/api/fetch-rotation$", "post_fetch_rotation"),
    (r"^/api/fetch-global$", "post_fetch_global"),
    (r"^/api/fetch-auction$", "post_fetch_auction"),
    (r"^/api/quant/backtest$", "post_quant_backtest"),
    (r"^/api/quant/strategies$", "post_quant_strategies_save"),
    (r"^/api/quant/strategies/get$", "post_quant_strategies_get"),
    (r"^/api/quant/strategies/delete$", "post_quant_strategies_delete"),
    (r"^/api/quant/job$", "post_quant_job"),
    (r"^/api/quant/compare$", "post_quant_compare"),
    (r"^/api/quant/rule/preview$", "post_quant_rule_preview"),
    (r"^/api/recap/note$", "post_recap_note"),
    (r"^/api/auction/watchlist$", "post_auction_watchlist"),
]
POST_ROUTES = [(re.compile(p), fn) for p, fn in POST_ROUTES]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=MAIN_WEB, **kwargs)

    # ---------------- 发送助手 ----------------

    def _cc(self, value):
        """发 Cache-Control 并标记「已自行管理缓存」，end_headers 不再补 no-cache。"""
        self._ak_cc = True
        self.send_header("Cache-Control", value)

    def send_response(self, *a, **kw):
        # 每个响应开始时重置缓存标记：上一响应若在 _cc() 之后、end_headers 之前抛异常，
        # keep-alive 连接的下一个请求不能继承「已自管缓存」的状态（会漏补 no-cache）。
        self._ak_cc = False
        super().send_response(*a, **kw)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        body, enc = self._maybe_gzip(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cc("no-store")
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_gzip(self, body):
        """客户端接受 gzip 且体足够大时压缩（手机/Tailscale 拉 420KB 全球数据可降 ~5-8x）。
        返回 (body, encoding)；不压缩时 encoding=None。"""
        if len(body) < 1024:
            return body, None
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in ae:
            return body, None
        return gzip.compress(body, 6), "gzip"

    def _etag_json(self, obj):
        """带 ETag/304 的 JSON：轮询方数据未变时不回传整包（/api/day 同款）。"""
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"%s"' % hashlib.md5(body).hexdigest()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", etag)
        body, enc = self._maybe_gzip(body)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype, cache="no-store"):
        """静态文件。cache=no-store（页面，刷新即见）/ etag（lib 常改文件，304 协商）
        / long（vendored 大文件，max-age 长缓存 + 304 兜底）。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
            st = os.stat(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        etag = '"%s"' % hashlib.md5(f"{st.st_size}-{st.st_mtime_ns}".encode()).hexdigest()
        if cache in ("etag", "long") and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self._cc("no-cache" if cache == "etag" else "public, max-age=86400, immutable")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        if cache == "long":
            self._cc("public, max-age=86400, immutable")
        elif cache == "etag":
            self._cc("no-cache")
        else:
            self._cc("no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, path):
        return _read_json_file(path)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return None

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # ---------------- 写接口闸门（2026-09-04 加固） ----------------

    def _client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _write_gate_ok(self):
        """POST 写接口闸门。返回 True 放行；返回 None 表示已拒绝（403/401 已发送）。

        1) CSRF：浏览器跨站 POST 必带 Origin（部分场景只带 Referer），与本服务
           Host 不同源 → 403。看板页面同源访问、手机经 Tailscale 同源访问不受影响。
        2) 局域网/外网直连（curl 等无浏览器头的客户端）：非本机来源在配置了
           AK_WRITE_TOKEN 时必须携带 X-AK-Token 头（或 ?token=）；未配置时保持
           旧行为放行（启动横幅提示如何开启）。本机来源始终放行。
        """
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        if origin or referer:
            src = origin or referer
            src_netloc = urllib.parse.urlparse(src).netloc
            if host and src_netloc.lower() != host.lower():
                self._json({"error": "cross-origin write rejected"}, 403)
                self.log_message("write rejected: cross-origin %s -> %s %s",
                                 src_netloc, host, self.path)
                return None
            return True   # 同源浏览器请求，放行
        # 无 Origin/Referer：非浏览器客户端
        if self._client_is_local():
            return True
        token = os.environ.get(WRITE_TOKEN_ENV, "").strip()
        if not token:
            return True   # 未配置 token：保持旧行为（启动横幅已提示）
        got = (self.headers.get("X-AK-Token") or ""
               or self._query().get("token", [""])[0])
        if got == token:
            return True
        self._json({"error": "write token required"}, 401)
        self.log_message("write rejected: missing token %s", self.path)
        return None

    # ---------------- 分发 ----------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        for pat, fn in GET_ROUTES:
            m = pat.fullmatch(path)
            if m:
                return getattr(self, fn)(**m.groupdict())
        if path.startswith("/api/") or path.startswith(("/data/", "/quant-results/")):
            # API 族未知路径统一结构化 404（与 POST 路由同口径），前端 fetch 拿到可解析的错误体
            return self._json({"error": "not found"}, 404)
        # 其余静态文件从主 web/ 提供
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        for pat, fn in POST_ROUTES:
            m = pat.fullmatch(path)
            if m:
                if self._write_gate_ok() is None:
                    return
                return getattr(self, fn)(**m.groupdict())
        return self._json({"error": "not found"}, 404)

    # ---------------- GET handlers：轮动 ----------------

    def api_dates(self):
        return self._json({"dates": rot_dates()})

    def api_health(self):
        return self._json(health_payload())

    def api_sentiment(self):
        return self._json(sentiment_payload())

    def api_board_members(self):
        name = self._query().get("name", [""])[0].strip()
        if not name or len(name) > 40:
            return self._json({"error": "板块名非法"}, 400)
        return self._json(board_members.payload(ROOT, name))

    def api_rotation_stats(self):
        self._maybe_auto_rotation_revive()   # 盘中断流自愈（闸门/节流见该函数）
        return self._json(rotation_stats_payload())

    def api_rotation_matrix(self):
        self._maybe_auto_rotation_revive()
        return self._json(rotation_matrix_payload())

    def api_day(self):
        dates = rot_dates()
        if not dates:
            return self._json({"error": "暂无轮动数据，盘中请先运行 backend/rotation/ths_collect.py"}, 404)
        date = self._query().get("date", [dates[-1]])[0]
        if not _DATE_RE.fullmatch(date):   # 白名单，防路径穿越
            return self._json({"error": "日期格式非法"}, 400)
        data = self._read_json(os.path.join(ROT_DAILY, f"{date}.json"))
        if data is None:
            return self._json({"error": f"没有 {date} 的数据"}, 404)
        # ETag/304：盘中轮询的数据未变化时不再回传整包分时 JSON
        return self._etag_json(data)

    def api_boards(self):
        boards = self._read_json(ROT_BOARDS)
        if boards is None:
            return self._json({"error": "暂无板块清单"}, 404)
        return self._json(boards)

    def api_quote(self):
        # 个股实时行情代理（同花顺 hithink 快照 + 单代码 5s TTL 服务端缓存），
        # 支持 600519 / 000001 / sh600519 / 北交所8xxxxx
        raw = self._query().get("codes", [""])[0]
        codes = []
        for c in raw.split(","):
            c = c.strip()
            if c and auc_config.to_thscode(c):
                codes.append(c)
        codes = list(dict.fromkeys(codes))  # 去重，保持顺序
        if not codes:
            return self._json({"error": "代码格式非法"}, 400)
        if len(codes) > 500:
            return self._json({"error": "最多 500 只"}, 400)
        return self._json(quote_service.quote_payload(codes, ROOT))

    def api_industry_map(self):
        # 个股→同花顺一级行业（全站单一来源；复盘页行业列/focus 链接用）
        return self._etag_json(industry_map_payload())

    # ---------------- GET handlers：复盘 ----------------

    def api_recap_dates(self):
        return self._json({"dates": recap_dates()})

    def api_recap_modules(self):
        return self._json({"modules": [
            {"key": k, "title": t, "allow_empty": e} for k, t, e in MODULE_REGISTRY]})

    def api_recap_note(self):
        date = self._query().get("date", [""])[0]
        if not _DATE8_RE.fullmatch(date):
            return self._json({"error": "日期格式非法"}, 400)
        fp = os.path.join(RECAP_NOTES, date + ".md")
        content = ""
        saved_at = None
        try:
            with open(fp, encoding="utf-8") as f:
                content = f.read()
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(os.path.getmtime(fp)))
        except OSError:
            pass
        return self._json({"date": date, "content": content, "saved_at": saved_at})

    def api_snapshot(self, name):
        # /data/YYYYMMDD.json：白名单已由路由正则保证（仅 8 位数字.json），防路径穿越
        data = snapio.load(name[:-5], RECAP_DATA)    # 自动兼容 .json.gz
        if data is None:
            return self._json({"error": "snapshot missing or broken"}, 500)
        return self._json(data)

    # ---------------- GET handlers：全球 / 量化 ----------------

    def api_global(self):
        data = self._read_json(GLOBAL_JSON)
        if data is None:
            return self._json({"error": "尚无全球总览数据，请先 POST /api/fetch-global 或运行 backend/global/fetch_global.py"}, 404)
        return self._json(data)

    def api_quant(self):
        force = self._query().get("fresh", [""])[0] in ("1", "true")
        return self._json(quant_payload(force=force))

    def api_quant_meta(self):
        try:
            return self._json(quant_api.meta(force=self._query().get("fresh", [""])[0] in ("1", "true")))
        except Exception as e:
            return self._json({"error": f"meta 失败：{e}"}, 500)

    def api_quant_strategies(self):
        try:
            return self._json({"strategies": quant_api.strategies_list()})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def api_quant_jobs(self):
        try:
            return self._json(quant_api.jobs_list())
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def api_quant_job_get(self, jid):
        try:
            return self._json(quant_api.job_get(jid))
        except Exception as e:
            return self._json({"error": str(e)}, 400)

    # ---------------- GET handlers：竞价 ----------------

    def api_auction(self):
        self._maybe_auto_sector_backfill()   # sector 停在旧日期时的自愈补抓（闸门/节流见该函数）
        return self._etag_json(auction_payload())

    def api_auction_series(self):
        return self._etag_json(auction_series_payload())

    def api_auction_status(self):
        # 真正的轻量：不读 live/final 整包（108~300 只 items 可达几百 KB），
        # 页面补抓期间每 5s 轮询一次，走这里而不是 /api/auction。
        st = load_status("auction.json")
        return self._json({"status": st, "watchlist": auc_config.read_watchlist(),
                           "fetching": lockutil.held_info(auc_config.LOCK_PATH) is not None,
                           "rounds": len((auc_config.load_json("rounds_meta.json") or {}).get("rounds") or []),
                           "config": {"interval": auc_config.INTERVAL, "batch": auc_config.BATCH,
                                      "max_codes": auc_config.MAX_CODES, "hot_top": auc_config.HOT_TOP}})

    # ---------------- GET handlers：静态 ----------------

    def api_lib_static(self, rest):
        """web/lib/ 共享资源：ETag 协商缓存；echarts/world.json 长缓存（immutable）。"""
        full = os.path.realpath(os.path.join(LIB_WEB, rest))
        if not full.startswith(os.path.realpath(LIB_WEB) + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = _LIB_CTYPES.get(ext)
        if not ctype:
            return self._json({"error": "not found"}, 404)
        url_path = "/lib/" + rest.replace("\\", "/")
        cache = "long" if url_path in _LIB_IMMUTABLE else "etag"
        return self._file(full, ctype, cache=cache)

    def api_quant_results(self, sub):
        # 量化自包含报告直链：/quant-results/<file> 与 /quant-results/research/<file>
        from urllib.parse import unquote
        sub = unquote(sub)
        if sub.startswith("research/"):
            root_dir, fname = os.path.join(quant_collector.QUANT_ROOT, "results", "research"), sub[len("research/"):]
        else:
            root_dir, fname = os.path.join(quant_collector.QUANT_ROOT, "results"), sub
        if not re.fullmatch(r"[\w.\-一-龥]+\.(html|json|csv)", fname or ""):
            return self._json({"error": "非法文件名"}, 400)
        fp = os.path.join(root_dir, fname)
        if not os.path.isfile(fp) or os.path.dirname(os.path.realpath(fp)) != os.path.realpath(root_dir):
            return self._json({"error": "not found"}, 404)
        ctype = {"html": "text/html; charset=utf-8",
                 "json": "application/json; charset=utf-8",
                 "csv": "text/csv; charset=utf-8"}[fname.rsplit(".", 1)[-1]]
        return self._file(fp, ctype)

    # ---------------- GET handlers：页面 ----------------

    def page_recap(self):
        return self._file(os.path.join(RECAP_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_global(self):
        return self._file(os.path.join(GLOBAL_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_auction(self):
        return self._file(os.path.join(AUCTION_WEB, "index.html"),
                          "text/html; charset=utf-8")

    def page_quant(self):
        return self._file(os.path.join(QUANT_WEB, "index.html"),
                          "text/html; charset=utf-8")

    # ---------------- POST handlers：后台抓取 ----------------

    def _spawn_fetch(self, script, cwd, logfile, lock, key, label, extra=None, respond=True):
        """启动后台抓取：文件锁防跨进程重入，日志写 .status/logs/。返回 _json 已处理则 True。

        锁纪律：_PROC_LOCK 只护 _fetch_procs 的读写与「查+占位」的原子性，
        Popen/写日志在锁外做——写 socket（409 响应）不能发生在持锁时，
        否则一个慢客户端能拖住所有并发抓取请求。占位先塞 None，
        Popen 成功后替换，异常时清位。
        respond=False 供内部自愈调用（sector 自动补抓）：同一套锁与进程簿记，
        但不占用请求响应——「已在跑/失败」静默返回，留给下轮轮询观察结果。"""
        held = lockutil.held_info(lock)
        if held:
            if respond:
                self._json({"status": "running", "held": held}, 409)
            return True
        with _PROC_LOCK:
            proc = _fetch_procs.get(key)
            if proc is not None and proc.poll() is None:
                if respond:
                    self._json({"status": "running"}, 409)
                return True
            _fetch_procs[key] = None   # 占位：同 key 并发请求在此等价于「已在跑」
        try:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
            log = open(logfile, "a", encoding="utf-8")
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] ==== {label} manual fetch start ====\n")
            log.flush()
            p = subprocess.Popen(
                [sys.executable, script, *(extra or [])],
                cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with _PROC_LOCK:
                _fetch_procs[key] = p
        except Exception as e:
            with _PROC_LOCK:
                _fetch_procs.pop(key, None)
            if respond:
                self._json({"status": "error", "error": str(e)}, 500)
            return True
        return False

    def _maybe_auto_sector_backfill(self):
        """板块竞价强度自愈（2026-09-04 事故）：竞价采集器盘中静默死亡会同时丢掉
        09:25:10 终态与挂在终态路径上的 sector 抓取 → sector.json 停在昨日 →
        前端退回「观察池行业聚合」偏样本口径冒充板块强度（当日实测：09:19:30 死亡，
        用户看到的「板块竞价强度」其实只是 70 只涨停池+热股的均值）。
        页面每 10s 轮询 /api/auction 时顺手检查：sector 非今日则后台补跑采集器
        --once（冻结终态 + 90 行业开盘缺口；open/prev 是日K属性，盘后补抓仍是今日真实值）。
        闸门按成本从低到高：内存 10 分钟节流 → 时间窗 09:26–23:00 → sector 已是今日 →
        采集锁空闲 → hithink 交易日历（子进程最贵，且必须 fail-closed：日历不可用宁可
        跳过——绝不能在非交易日把昨日冻结值写成今日 final）。"""
        now = time.time()
        if now - _auto_sector_ts[0] < 600:
            return
        hm = time.strftime("%H:%M")
        if not ("09:26" <= hm <= "23:00"):
            return
        try:
            sec = auc_config.load_json("sector.json") or {}
        except Exception:
            return
        if sec.get("date") == time.strftime("%Y%m%d"):
            return
        if lockutil.held_info(auc_config.LOCK_PATH):
            return   # 采集在跑（live 轮询或上一次补抓），不打断
        _auto_sector_ts[0] = now   # 无论后续成败，10 分钟内不再试探（含日历子进程的成本闸）
        try:
            import trade_cal  # noqa: PLC0415  交易日历判定单一来源（backend/trade_cal.py）
            if trade_cal.is_trade_today() is not True:
                return   # False=非交易日 / None=日历不可用：一律 fail-closed
        except Exception:
            return
        self._spawn_fetch(
            AUCTION_FETCH_SCRIPT, AUCTION_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-auction.log"), auc_config.LOCK_PATH,
            "auction", "auction", extra=["--once"], respond=False)

    def _maybe_auto_rotation_revive(self):
        """轮动断流自愈（2026-09-05）：盘中采集进程死亡会让当段分钟永久缺失
        （同花顺口径没有分钟级指数接口，不可回补），rotation_stall() 只报警不补救。
        页面轮询 /api/rotation-* 时顺手检查：stalled 且采集锁心跳也断 >6 分钟
        （活循环每轮 utime 心跳，断 6 分钟=持有者必死）→ 摘死锁、后台拉起常驻
        循环（循环模式自动补采至 15:00 收盘定格后自退出）。闸门按成本从低到高：
        10 分钟节流 → 交易时段 09:36–14:55（之后尾部由定格任务+定格并入兜底）→
        stall 确认 → 锁心跳确认死 → 交易日历 fail-closed（子进程最贵，日历不可用
        宁可跳过——绝不在非交易日拉起采集）。机器关机与上游长时间故障仍属物理缺口。"""
        now = time.time()
        if now - _auto_rot_ts[0] < 600:
            return
        hm = time.strftime("%H:%M")
        if not ("09:36" <= hm <= "14:55"):
            return
        st = rotation_stall()
        if not (st and st.get("stalled")):
            return
        try:
            lock_age = time.time() - os.path.getmtime(LOCK_ROT)
        except OSError:
            lock_age = None
        if lock_age is not None and lock_age < 360:
            return   # 锁心跳 <6 分钟：持有者大概率活着（可能刚被别的路径拉起），不动
        _auto_rot_ts[0] = now   # 无论后续成败，10 分钟内不再试探（含日历子进程的成本闸）
        try:
            import trade_cal  # noqa: PLC0415  交易日历判定单一来源（backend/trade_cal.py）
            if trade_cal.is_trade_today() is not True:
                return   # False=非交易日 / None=日历不可用：一律 fail-closed
        except Exception:
            return
        lockutil.release(LOCK_ROT)   # 摘死锁（活持有者不可能出现：心跳断 6 分钟必死）
        self._spawn_fetch(
            ROT_FETCH_SCRIPT, ROT_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-rotation.log"), LOCK_ROT,
            "rotation", "rotation", extra=None, respond=False)

    def _fetch_started(self):
        return self._json({"status": "started"})

    def post_fetch_recap(self):
        done = self._spawn_fetch(
            FETCH_SCRIPT, FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-recap.log"), LOCK_RECAP,
            "recap", "recap")
        return self._fetch_started() if not done else None

    def post_fetch_rotation(self):
        done = self._spawn_fetch(
            ROT_FETCH_SCRIPT, ROT_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-rotation.log"), LOCK_ROT,
            "rotation", "rotation", extra=["--once"])
        return self._fetch_started() if not done else None

    def post_fetch_global(self):
        done = self._spawn_fetch(
            GLOBAL_FETCH_SCRIPT, GLOBAL_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-global.log"), LOCK_GLOBAL,
            "global", "global")
        return self._fetch_started() if not done else None

    def post_fetch_auction(self):
        done = self._spawn_fetch(
            AUCTION_FETCH_SCRIPT, AUCTION_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-auction.log"), auc_config.LOCK_PATH,
            "auction", "auction", extra=["--once"])
        return self._fetch_started() if not done else None

    # ---------------- POST handlers：量化 ----------------

    def post_quant_backtest(self):
        req = self._body()
        if req is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        try:
            return self._json(quant_api.run_backtest_api(req))
        except Exception as e:
            return self._json({"error": str(e)[:600]}, 400)

    def post_quant_strategies_save(self):
        req = self._body()
        if req is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        try:
            return self._json(quant_api.strategies_save(req))
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_strategies_get(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.strategies_get(req.get("name", "")))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 404)

    def post_quant_strategies_delete(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.strategies_delete(req.get("name", "")))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)

    def post_quant_job(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.job_start(req.get("kind", ""), req.get("params") or {}))
        except quant_api.BlockingError as e:
            return self._json({"error": str(e)}, 409)
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_compare(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.compare_api(req))
        except Exception as e:
            return self._json({"error": str(e)[:400]}, 400)

    def post_quant_rule_preview(self):
        req = self._body() or {}
        try:
            return self._json(quant_api.rule_preview(req.get("spec") or req))
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 400)

    # ---------------- POST handlers：复盘笔记 / 竞价观察池 ----------------

    def post_recap_note(self):
        date = self._query().get("date", [""])[0]
        if not _DATE8_RE.fullmatch(date):
            return self._json({"error": "日期格式非法"}, 400)
        body = self._body()
        if body is None:
            return self._json({"error": "body 需为 JSON"}, 400)
        content = body.get("content")
        if not isinstance(content, str):
            return self._json({"error": "content 需为字符串"}, 400)
        if len(content) > 200000:
            return self._json({"error": "笔记过长（>200KB）"}, 400)
        os.makedirs(RECAP_NOTES, exist_ok=True)
        fp = os.path.join(RECAP_NOTES, date + ".md")
        try:
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(tmp, fp)
        except Exception as e:
            return self._json({"status": "error", "error": str(e)}, 500)
        return self._json({"status": "saved",
                           "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    def post_auction_watchlist(self):
        req = self._body() or {}
        text = req.get("text")
        if not isinstance(text, str) or len(text) > 20000:
            return self._json({"error": "text 需为字符串（≤20000 字符）"}, 400)
        _, invalid = auc_config.parse_watchlist(text)   # 先解析出被丢弃的 token，别静默少股
        codes = auc_config.write_watchlist(text)
        return self._json({"status": "saved", "codes": codes, "total": len(codes),
                           "invalid": invalid[:20], "invalid_total": len(invalid),
                           "note": "下次采集自动生效；盘中可点「立即补抓」尽快落地"})

    # ---------------- 其它 ----------------

    def end_headers(self):
        # 我们自己的发送器都走 _cc() 自管缓存；只有 SimpleHTTPRequestHandler 托管的
        # 其余静态文件（favicon 等）在这里补 no-cache，保证样式/页面更新后刷新即见。
        if not getattr(self, "_ak_cc", False):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):  # 更安静
        sys.stderr.write(f"[server] {self.address_string()} {fmt % args}\n")


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="A股看板统一服务（竞价/轮动/复盘/全球/量化）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AK_PORT") or 8000))
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("!! 绑定了非本机地址：接口将暴露给局域网。写接口已有跨源防护（Origin/Referer）,")
        print("!! 但局域网内的脚本直连仍无鉴权；如需收紧，设置环境变量 AK_WRITE_TOKEN=随机串，")
        print("!! 之后非本机写请求必须携带 X-AK-Token 头。")

    rd, cd = rot_dates(), recap_dates()
    landing = landing_path()
    print("=" * 56)
    print("A股看板 · 统一服务（竞价 + 轮动 + 复盘 + 全球 + 量化）")
    print(f"  竞价数据: {auction_brief()}")
    print(f"  轮动数据: {', '.join(rd[-3:]) if rd else '（无，先运行 backend/rotation/ths_collect.py）'}")
    print(f"  复盘数据: {', '.join(cd[:3]) if cd else '（无，先运行 backend/recap/fetch_daily.py）'}")
    print(f"  访问: http://{args.host}:{args.port}{landing}   （竞价: /auction ｜ 轮动: / ｜ 复盘: /recap ｜ 全球: /global ｜ 量化: /quant）")
    print(f"  落点: {landing_describe()}")
    print("  Ctrl+C 退出")
    print("=" * 56)
    with ThreadingServer((args.host, args.port), Handler) as httpd:
        if not args.no_open:
            import webbrowser
            webbrowser.open(f"http://{args.host}:{args.port}{landing}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")


if __name__ == "__main__":
    main()
