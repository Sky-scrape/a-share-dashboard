# -*- coding: utf-8 -*-
"""A股看板 · 服务上下文（server.py 拆分 · 方案 O-3b，2026-09-27）。

单文件 server.py 涨到 ~1660 行后，改任何一处都要把全站上下文载入注意力。
按路由域拆分后，本模块集中「除 Handler 方法、GET/POST 路由表、main() 之外的
一切」：backend 引导（env 代理清空 + sys.path 插入 + backend 导入，顺序与旧
单文件 server.py 逐行一致）、全部模块级常量（路径/锁/进程内缓存字典/
_LIB_CTYPES 等）与独立 helper（health_payload/_health_alerts/auction_payload/
_hithink_probe/各缓存读取器等）。

依赖方向（防循环）：server.py → server_handlers_*.py → server_context → backend；
本模块绝不 import server，server_handlers_* 亦然。
"""
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time

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
import fsutil          # noqa: E402  原子写盘单一来源（backend/fsutil.py）
import notify          # noqa: E402  告警推送单一出口（backend/notify.py）
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
import version as ak_version         # noqa: E402  版本单一来源（backend/version.py）

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
READ_TOKEN_ENV = "AK_READ_TOKEN"     # 配置后非本机对 /api、/data、/quant-results 的
                                     # 全部请求（读+写）都必须携带 X-AK-Token（或 ?token=）；
                                     # 本机与页面静态资源不受影响。远程暴露场景才需要。

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
    # 午休豁免（11:30–13:00 停采是正常的，采集器心跳/落盘都停，见 ths_collect 的
    # AM_CLOSE/PM_OPEN 常量）：不豁免会把午休误报成断流，页面假警 + 诱发自愈摘活锁。
    # PM 侧 +5 分钟宽限 = 停滞阈值本身（13:00 恢复采样的头几分钟 raw 还停在 11:30）。
    if 11 * 60 + 30 < hm < 13 * 60 + 5:
        return {"stalled": False, "age_minutes": None,
                "note": "午间休市（11:30–13:00 停采为正常）"}
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
        # 解析失败回退旧缓存要诚实标注：让消费方知道这是上一份好面板，
        # 而不是当前文件的真实内容（数据边界诚实原则，同 fetch_global._keep）。
        if ent is not None and isinstance(ent[1], dict):
            prev = dict(ent[1])
            prev["stale"] = True
            return prev
        return ent[1] if ent else None
    with _CACHE_LOCK:
        _panel_cache[path] = (mtime, obj)
    return obj


# ---------------- 大 JSON 文件的「序列化 bytes + ETag」mtime 缓存 ----------------
# /api/day（~800KB，轮动页 15s 轮询 + 复盘页 5 连拉）此前每请求都要
# json.load 全量解析再 dumps 算 ETag；缓存下沉到序列化层后，
# 文件 mtime+size 不变时 304 与 200 都近零成本（零读盘零解析零重序列化）。
_etag_body_cache = {}   # path -> [(mtime_ns, size), body_bytes, etag, [gz_bytes 或 None]]
_ETAG_BODY_CACHE_MAX = 64   # /api/day 可带任意历史 date（每条约 1MB body+gzip），封顶防回看累积


def _cached_json_bytes(path):
    """纯 JSON 文件 → (紧凑序列化 bytes, ETag, gzip 槽位)，按 mtime+size 缓存。

    锁只护字典读写；解析/序列化在锁外做（与 _read_panel 同一纪律）。
    gzip 槽位惰性填充：首个接受 gzip 的 200 请求算一次并缓存，之后复用。
    超上限按 LRU 淘汰（命中即移到队尾）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        ent = _etag_body_cache.get(path)
        if ent and ent[0] == key:
            _etag_body_cache[path] = _etag_body_cache.pop(path)   # LRU：移到队尾
            return ent[1], ent[2], ent[3]
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        # 读失败（写半截等）：有过缓存就沿旧值，否则按无数据处理
        return (ent[1], ent[2], ent[3]) if ent else None
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # ETag 用 SHA-256（Mimosa 弱哈希清零；ETag 本非安全原语，换算法仅一次性 304 失效）
    etag = '"%s"' % hashlib.sha256(body).hexdigest()
    ent = [key, body, etag, [None]]   # 槽位用可变列表：gzip 结果可回填复用
    with _CACHE_LOCK:
        _etag_body_cache[path] = ent
        while len(_etag_body_cache) > _ETAG_BODY_CACHE_MAX:
            _etag_body_cache.pop(next(iter(_etag_body_cache)))
    return body, etag, ent[3]


def _snapshot_payload(date8):
    """复盘快照（/data/YYYYMMDD.json，~420KB，兼容 .json.gz）mtime 缓存解析。

    快照按日期不可变、抓取窗口内才整体重写：解压+解析只在文件变化后做一次，
    之后走 _etag_json 的 304 协商。"""
    p = snapio.snap_path(RECAP_DATA, date8)
    data = _read_panel(p, lambda x: snapio.load(date8, RECAP_DATA))
    return data


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

_recap_err_cache = {"key": None, "errors": ()}


def _recap_errors_cached(cap_path):
    """复盘快照 error 字段的 mtime+size 缓存。

    /api/health 被新鲜度胶囊 2 分钟轮询一次；此前每次都全量解压解析 ~420KB
    快照只为取 error 明细。文件不变时直接复用上次结果（模式照 _read_panel）。"""
    try:
        key = (cap_path, os.path.getmtime(cap_path), os.path.getsize(cap_path))
    except OSError:
        return []
    with _CACHE_LOCK:
        if _recap_err_cache["key"] == key:
            return list(_recap_err_cache["errors"])
    try:
        d = snapio.load(os.path.basename(cap_path)[:8]) or {}
        errors = [
            {"module": k, "error": (v.get("error") or "")[:120]}
            for k, v in (d.get("modules") or {}).items()
            if v.get("status") == "error"]
    except Exception:
        errors = []
    with _CACHE_LOCK:
        _recap_err_cache.update(key=key, errors=errors)
    return errors


def _us_market_health():
    """隔夜美股因子健康（data/recap/us_market/factors.json，只读不 import 模块）。

    2026-09-14 前该链路无任何直接监控：夜间抓取失败只能靠 speculate 备选池守卫
    文案间接暴露。这里给出覆盖末端 T、对齐的 us_date、updated 年龄与失败符号清单
    （us_market 抓取部分失败时会把 failed 清单写进 factors.json）。"""
    p = os.path.join(RECAP_DATA, "us_market", "factors.json")
    try:
        with open(p, encoding="utf-8") as f:
            fac = json.load(f)
    except FileNotFoundError:
        return {"present": False}
    except Exception:
        return {"present": True, "error": "factors.json 解析失败"}
    rows = fac.get("rows") or {}
    ks = sorted(rows)
    last_row = (rows.get(ks[-1]) or {}) if ks else {}
    return {
        "present": True,
        "updated_at": fac.get("updated"),
        "age_hours": _hours_since(fac.get("updated")),
        "last_t": ks[-1] if ks else None,
        "last_us_date": last_row.get("us_date"),
        "failed": list(fac.get("failed") or []),
    }


def _duckdb_health():
    """本地研究库（hithink DuckDB）日线层健康（.status/duckdb.json，只读不 import 模块）。

    2026-09-15 前该链路无监控：上游 T 日 release 未按时发布 → 17:05 sync 判 SKIP
    → 次日 04:05 终版链在缺 T 日线的库上重建池/验证，全部静默降级（0914 实例：
    低吸组空、偏离空、验证全标「停牌/数据缺失」）。speculate 侧每次探测原子落盘，
    这里直接读；stale=True 表示最近一次链跑时库内没有验证日（probed）的日线层。"""
    st = load_status("duckdb.json")
    if not st:
        return {"present": False}
    return {"present": True, "updated_at": st.get("updated"),
            "age_hours": _hours_since(st.get("updated")),
            "probed": st.get("probed"), "ok": bool(st.get("ok")),
            "layer_n": st.get("layer_n"), "last_bar": st.get("last_bar"),
            "fallback": st.get("fallback"), "db_error": st.get("db_error"),
            "note": st.get("note"),
            "stale": not st.get("ok", True)}


def _backup_health():
    """数据备份状态（.status/backup_state.json，backend/backup.py 落盘）。

    2026-09-15 前该资产零备份零监控：data/ 与 strategy-iter 研究库全在
    .gitignore 里，丢失不可再生。ok=False 或超 3 天没成功备份时给 stale=True
    （看门狗巡检据此推送，健康面板同步亮牌）。"""
    st = load_status("backup_state.json")
    if not st:
        return {"present": False, "note": "尚未运行过备份（python backend/backup.py）"}
    age_h = _hours_since(st.get("at"))
    stale = (st.get("ok") is False) or (age_h is not None and age_h > 72)
    return {"present": True, "ok": bool(st.get("ok")), "at": st.get("at"),
            "age_hours": age_h, "size_mb": st.get("size_mb"),
            "files": st.get("files"), "dest": st.get("dest"),
            "kept": st.get("kept"), "last_zip": st.get("last_zip"),
            "error": st.get("error"), "stale": stale}


# ---------------- hithink CLI 健康探活（方案 S-4，2026-09-27） ----------------
# 主力数据源此前在健康面板不是一等公民：key 失效是慢性病——症状（某几个模块
# error）与病因（auth 过期/CLI 被卸载）离得远，只能靠人肉归因。auth status 是
# 本地探活（不依赖交易时段），做成 TTL 缓存：巡检线程按节奏刷新，health 请求
# 只读缓存（探活有 subprocess 开销，绝不逐请求跑；缓存过期由后台线程补）。


_HT_PROBE = {"at": 0.0, "data": None, "probing": False}
_HT_PROBE_TTL = 600.0   # 与 _HEALTH_SCAN_INTERVAL 同节奏
_HT_LOCK = threading.Lock()


def _hithink_probe():
    """跑一次 auth status，结果（含失败）原子落入 _HT_PROBE。任何异常都视为
    探活结果而非错误——探活失败本身就是最重要的健康信号。"""
    try:
        import ht as ht_mod   # backend/recap 在 sys.path（server 启动时已插入）
        t0 = time.monotonic()
        data = ht_mod.ht("auth", "status", timeout=15)
        rec = {"present": True, "ok": True,
               "latency_ms": int((time.monotonic() - t0) * 1000),
               "detail": ({k: data.get(k) for k in ("user", "plan", "expire")
                           if isinstance(data, dict) and data.get(k)} or None)}
    except Exception as e:  # noqa: BLE001 - 见函数头：失败如实记录
        rec = {"present": True, "ok": False,
               "error": f"{type(e).__name__}: {str(e)[:160]}"}
    with _HT_LOCK:
        _HT_PROBE.update(at=time.time(), data=rec, probing=False)
    return rec


def _hithink_refresh_async():
    """过期/缺失时后台补探，绝不阻塞 health 请求。"""
    with _HT_LOCK:
        if _HT_PROBE["probing"]:
            return
        _HT_PROBE["probing"] = True
    threading.Thread(target=_hithink_probe, daemon=True, name="ht-probe").start()


def _hithink_health():
    """health.hithink 段：缓存读取 + 过期标记；首个请求只会看到 probing。"""
    with _HT_LOCK:
        at, rec = _HT_PROBE["at"], _HT_PROBE["data"]
    if rec is None:
        _hithink_refresh_async()
        return {"present": True, "probing": True,
                "note": "首次探活进行中（auth status）"}
    out = dict(rec)
    age = time.time() - at
    out["age_seconds"] = int(age)
    out["stale"] = age > _HT_PROBE_TTL
    if out["stale"]:
        _hithink_refresh_async()
    return out


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
    recap_errors = _recap_errors_cached(cap_path) if cap_path else []
    glob_d = _read_json_file(GLOBAL_JSON) if os.path.exists(GLOBAL_JSON) else None
    glob_ft = (glob_d or {}).get("fetched_at") or ""
    glob_errors = list((glob_d or {}).get("errors") or {})
    glob_stale = sorted(k for k, v in ((glob_d or {}).items())
                        if isinstance(v, dict) and v.get("stale"))
    with _PROC_LOCK:
        gp_proc = _fetch_procs.get("global")
    auc_st = load_status("auction.json")
    auc_live = auc_config.load_json("live.json") or {}
    auc_final = auc_config.load_json("final.json") or {}
    return {
        "service": "ak-dashboard",   # start.py 端口占用时校验对端身份用
        "version": ak_version.__version__,   # 版本单一来源 backend/version.py
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
            "stale_blocks": glob_stale,   # 沿用上一份的块（_keep），非本次实抓
            "fetching": (lockutil.held_info(LOCK_GLOBAL) is not None
                         or (gp_proc is not None and gp_proc.poll() is None)),
        },
        "us_market": _us_market_health(),
        "duckdb": _duckdb_health(),
        "hithink": _hithink_health(),
        "notify": notify.status(),
        "backup": _backup_health(),
        "auction": {
            "last_run": (auc_st or {}).get("last_run"),
            "date": (auc_st or {}).get("date"),
            "rounds": (auc_st or {}).get("rounds"),
            "live_updated_at": auc_live.get("updated_at"),
            "final_updated_at": auc_final.get("fetched_at"),
            "fetching": lockutil.held_info(auc_config.LOCK_PATH) is not None,
        },
    }


# ---------------- health 巡检推送（2026-09-15） ----------------
# 动机（0914 事故复盘）：告警此前只活在看板页面上，凌晨/盘后链路失败时没人看着
# 页面。守护线程每 10 分钟扫一次 health payload，把新出现的告警推到手机
# （backend/notify.py，key 级 6h 节流防轰炸）；服务本身死亡的场景由
# arecap-watchdog 计划任务（backend/watchdog.py）兜底——死人不能喊救命。

_HEALTH_SCAN_INTERVAL = 600


def _health_alerts(h):
    """health payload → [(key, 描述)]。判定全部复用各健康段的现成字段，
    不另立第二套口径；无告警返回空表。"""
    out = []
    stall = ((h.get("rotation") or {}).get("stall") or {})
    if stall.get("stalled"):
        out.append(("rotation.stall",
                    "盘中轮动采集停滞：" + str(stall.get("note") or "超过 5 分钟无新数据点")))
    ddb = h.get("duckdb") or {}
    if ddb.get("present") and ddb.get("stale"):
        out.append(("duckdb.stale",
                    f"研究库日线层缺失/异常（{ddb.get('note') or ddb.get('db_error') or '探测层为空'}），"
                    "腾讯备源兜底中——次日复盘可能部分降级"))
    htst = h.get("hithink") or {}
    if htst.get("present") and htst.get("ok") is False and not htst.get("probing"):
        out.append(("hithink.down",
                    f"主力数据源 hithink CLI 探活失败（{htst.get('error') or '未知'}）。"
                    "复盘/竞价/轮动抓取都会失败——请检查：hithink-finance auth status"
                    "（key 失效/过期重新 auth login；CLI 缺失则 npm install -g hithink-finance）"))
    um = h.get("us_market") or {}
    if um.get("present") and (um.get("age_hours") or 0) > 30:
        out.append(("us_market.stale",
                    f"隔夜美股因子过期 {um.get('age_hours')}h（updated {um.get('updated_at') or '-'}），"
                    "环境闸门可能用到旧值"))
    gsb = (h.get("global") or {}).get("stale_blocks") or []
    if gsb:
        out.append(("global.stale",
                    "全球总览部分块沿用旧值：" + ", ".join(str(x) for x in gsb[:8])))
    bk = h.get("backup") or {}
    if bk.get("present") and bk.get("stale"):
        out.append(("backup.stale",
                    f"数据备份异常或超期（最近 {bk.get('at') or '无'}，"
                    f"error={bk.get('error') or '-'}，目录 {bk.get('dest') or '-'}）"))
    # 竞价/复盘「今天该有而没有」类告警：交易日免误报复用竞价采集器的
    # non-trade-day 标记（09:14 采集器已查过交易日历），不重复联网判定
    now = time.localtime()
    if now.tm_wday < 5:
        hm = time.strftime("%H:%M")
        auc_st = load_status("auction.json") or {}
        holiday = str(auc_st.get("date") or "") == time.strftime("%Y%m%d") \
            and "non-trade-day" in str(auc_st.get("note") or "")
        if not holiday:
            auc = h.get("auction") or {}
            if "09:26" <= hm <= "15:10" \
                    and str(auc.get("date") or "") != time.strftime("%Y%m%d"):
                out.append(("auction.missing",
                            f"竞价采集今日尚无状态（最近 {auc.get('date') or '-'}），"
                            "检查计划任务 auction_task.bat"))
            rc = h.get("recap") or {}
            if "19:30" <= hm <= "23:59" \
                    and rc.get("last_date") != time.strftime("%Y-%m-%d"):
                out.append(("recap.missing",
                            f"17:05 复盘采集今日未见落盘（最新 {rc.get('last_date') or '-'}），"
                            "检查 data/fetch.log 与计划任务 arecap-daily-fetch"))
    return out


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


# ---------------- 静态资源缓存策略（原路由表区块内的常量，随方案 O-3b 迁入） ----------------

# 大体积、几乎不变的 vendored 资源长缓存（1MB echarts / 1MB world.json 每次刷新全量重拉
# 与「为手机省带宽」的 gzip 优化自相矛盾）；其余 lib 文件开发期常改，只给 ETag 协商缓存。
_LIB_IMMUTABLE = {"/lib/echarts.min.js", "/lib/map/world.json"}
_LIB_CTYPES = {".js": "text/javascript; charset=utf-8",
               ".css": "text/css; charset=utf-8",
               ".json": "application/json; charset=utf-8",
               ".svg": "image/svg+xml",
               ".png": "image/png",
               ".woff2": "font/woff2"}
