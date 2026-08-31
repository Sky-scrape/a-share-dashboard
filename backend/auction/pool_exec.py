# -*- coding: utf-8 -*-
"""昨日备选池 × 今日竞价 → 执行判定（2026-09-04 新增，竞价页「昨日备选池·执行」卡）。

策略结论「纪律执行是收益的一半」（C_Final：低吸组触发 +5.08% vs 未触发 -3.30%）此前
全靠人肉兑现——早上翻复盘页记票、再去竞价页对照。本模块把闭环接上：取上一交易日
复盘快照的备选池（C_Final picks），对照今日竞价逐轮（series.json 窗口内轮次），
按**冻结执行层**（backend/execution_layer.py，与验证器/最终方案同一份窗口）给出
实时判定：低开放弃 / 高开超窗放弃 / 窗内观察（附盘中纪律要点）。

配套「情绪前瞻」信号（strategy-iter gap_ahead_study.py 回验，2026-09-04）：
昨日涨停池今日竞价低开占比 >50% 的 12 个交易日（日线近似口径），当日 C_Final 备选池
实时口径日均益 -1.08%（其余日 +0.4~+1.1%），空仓模拟收益 +17pct / 回撤改善 5.3pct；
**样本小（n=12）且为日线近似口径，只作提示、不改环境配额与结构规则**——改规则须走
《自动选股与策略自迭代系统.md》完整重跑。

HTTP 层不做业务计算：本模块只被 server.py 的 auction_payload 调用组装 payload；
payload 不含时间戳（ETag 轮询机制依赖内容稳定）。纯函数 compute 可单测。
"""
import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE),                      # backend/（execution_layer）
           os.path.abspath(os.path.join(_HERE, os.pardir, "recap"))):  # backend/recap（snapio）
    if _p not in sys.path:
        sys.path.insert(0, _p)

import snapio          # noqa: E402  快照读写单一入口（backend/recap）
import execution_layer # noqa: E402  冻结执行层单一来源

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "data", "auction")
SERIES_PATH = os.path.join(DATA_DIR, "series.json")
WATCHMAP_PATH = os.path.join(DATA_DIR, "watchmap.json")
FINAL_PATH = os.path.join(DATA_DIR, "final.json")

# 情绪前瞻阈值与历史证据（只提示不改规则，理由见模块头注）
AHEAD_OPEN_DOWN_WARN = 0.50
AHEAD_STUDY_NOTE = ("回验(日线近似): 昨涨停低开占比>50% 桶 n=12 当日池均益 -1.08%，"
                    "其余日 +0.4~+1.1%；样本小仅提示")

_GRP_CN = {"lu": "涨停组", "nlu": "低吸组"}

_LOCK = threading.Lock()
_CACHE = {"key": None, "payload": None}


def _latest_pool_date(today8):
    """今天之前的最近一个复盘快照日（snapio.list_dates 倒序，取第一个更早的）。"""
    dates = sorted((d for d in snapio.list_dates() if d < today8), reverse=True)
    return dates[0] if dates else None


def _load_pool(pool_date):
    """快照日 → (pool dict, picks 行)；缺失/损坏返回 ({}, [])。"""
    try:
        snap = snapio.load(pool_date)
    except Exception:  # noqa: BLE001
        return {}, []
    mod = ((snap or {}).get("modules") or {}).get("speculation") or {}
    pool = ((mod.get("data") or {}).get("pool")) or {}
    return pool, (pool.get("picks") or [])


def _series():
    try:
        with open(SERIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"date": None, "rounds": []}


def _final_round(series):
    """可信定盘轮：final.json 与 series 同日且 09:26 前抓取（前端 renderFlip 同款门卫）。

    午后补抓写进 final 的是现价快照（2026-09-04 实测 65/70 只被现价污染），绝不能当
    09:25 撮合值；不可信时返回 None（判定退回窗口内最后一轮的暂定值，诚实标注）。
    """
    try:
        with open(FINAL_PATH, encoding="utf-8") as f:
            fin = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    t = str(fin.get("fetched_at") or "")[11:19]
    if fin.get("date") != series.get("date") or not t or t > "09:26:00":
        return None
    rnd = fin.get("round") or {}
    return rnd if (rnd.get("items") or []) else None


def _in_window(r):
    """与前端 roundInWindow 同口径：显式标记优先，旧数据按 ts 回推。"""
    if isinstance(r.get("in_window"), bool):
        return r["in_window"]
    t = str(r.get("ts") or "")[11:19]
    return len(t) == 8 and "09:15:00" <= t <= "09:25:59"


def _latest_pcts(series, extra_round=None):
    """窗口内轮次（按 ts 升序，含可选的可信定盘轮）逐票最后有效竞价涨幅。

    返回 ({code6: pct}, 末轮时刻, 末轮是否定盘)。
    """
    rounds = sorted((r for r in (series.get("rounds") or []) if _in_window(r)),
                    key=lambda r: str(r.get("ts") or ""))
    if extra_round:
        rounds.append(extra_round)
        rounds.sort(key=lambda r: str(r.get("ts") or ""))
    out = {}
    for r in rounds:
        for it in (r.get("items") or []):
            p = it.get("auction_pct")
            if p is not None:
                out[str(it.get("thscode") or "").split(".")[0]] = p
    last_ts = str(rounds[-1].get("ts"))[11:19] if rounds else None
    return out, last_ts, bool(extra_round) and rounds and rounds[-1] is extra_round


def _ahead_signal(series, watchmap, extra_round=None):
    """情绪前瞻：昨日涨停池（watchmap 来源含 L）今日竞价低开/深低开占比。

    watchmap 键为完整 thscode（如 601086.SH，auc 侧写入口径），6 位代码兜底一次。
    """
    rounds = sorted((r for r in (series.get("rounds") or []) if _in_window(r)),
                    key=lambda r: str(r.get("ts") or ""))
    if extra_round:
        rounds.append(extra_round)
        rounds.sort(key=lambda r: str(r.get("ts") or ""))
    if not rounds:
        return None
    items = (rounds[-1].get("items") or [])
    lu = []
    for it in items:
        tc = str(it.get("thscode") or "")
        src = str(((watchmap or {}).get(tc)
                   or (watchmap or {}).get(tc.split(".")[0]) or {}).get("s") or "")
        if "L" in src:
            lu.append(it)
    pcts = [it.get("auction_pct") for it in lu if it.get("auction_pct") is not None]
    if not pcts:
        return None
    down = sum(1 for p in pcts if p < 0) / len(pcts)
    deep = sum(1 for p in pcts if p <= -2) / len(pcts)
    return {"lu_n": len(pcts), "open_down": round(down, 3), "open_deep": round(deep, 3),
            "warn_threshold": AHEAD_OPEN_DOWN_WARN,
            "warn": down > AHEAD_OPEN_DOWN_WARN, "study": AHEAD_STUDY_NOTE}


def _window_text(grp):
    b = execution_layer.MF_BUY[grp]
    return f"开盘 {b['win_min']:+g}%~{b['win_max']:+g}%"


def _verdict(grp, pct):
    """(kind, verdict, plan)：kind ∈ ok / skip / missing。口径=冻结执行层。"""
    b = execution_layer.MF_BUY[grp]
    if pct is None:
        return "missing", "未入竞价池", "按开盘价人工对照窗口"
    if pct < b["win_min"]:
        return "skip", ("低开·放弃" if grp == "lu" else "低开超窗·放弃"), "纪律：低开不接"
    if pct > b["win_max"]:
        return "skip", "高开超窗·放弃", "纪律：防情绪兑现"
    if grp == "lu":
        return "ok", "窗内·观察", "盘中破昨收-3%放弃；收盘站上开盘才算确认"
    return "ok", "窗内·挂上穿", "价格上穿 max(开盘,昨收) 再接；收盘站不上不买"


def compute(picks, series, watchmap=None, final_round=None):
    """纯函数：昨日池 picks × 今日竞价 series（+可选可信定盘轮）→ 执行卡 payload。"""
    if not picks:
        return {"ok": False, "note": "上一交易日无备选池标的（冰点/缺快照）", "rows": []}
    pcts, last_ts, is_final = _latest_pcts(series, extra_round=final_round)
    rows, counts = [], {"ok": 0, "skip": 0, "missing": 0}
    for p in picks:
        grp = "nlu" if p.get("类型") == "非涨停板" else "lu"
        code = str(p.get("代码") or "")
        pct = pcts.get(code)
        kind, verdict, plan = _verdict(grp, pct)
        counts[kind] += 1
        rows.append({"code": code, "name": p.get("名称"), "grp": grp,
                     "grp_cn": _GRP_CN[grp], "concept": p.get("所属概念") or "-",
                     "score": p.get("得分"), "window": _window_text(grp),
                     "pct": pct, "kind": kind, "verdict": verdict, "plan": plan})
    return {"ok": True, "rows": rows, "counts": counts,
            "last_ts": last_ts, "final": is_final,
            "signal": _ahead_signal(series, watchmap, extra_round=final_round),
            "note": "窗口=冻结执行层（与验证器/最终方案同一份）；竞价未结束时涨幅为暂定值"}


def payload(today8=None):
    """server.auction_payload 用：自带缓存（series/快照/watchmap 任一变化才重算）。

    缓存键只用各文件的 mtime——**先算键、命中直接返回**，绝不为了判断是否变化
    而整读快照（/api/auction 是 10s 轮询接口，整读数 MB 的 gz 会把缓存打穿）。
    payload 无时间戳字段——ETag 轮询依赖内容稳定。
    """
    try:
        today8 = today8 or time.strftime("%Y%m%d")
        pool_date = _latest_pool_date(today8)
        snap_fp = snapio.snap_path(snapio.RECAP_DATA, pool_date) if pool_date else None

        def _mtime(fp):
            try:
                return os.path.getmtime(fp)
            except OSError:
                return None
        key = (pool_date, _mtime(snap_fp) if snap_fp else None,
               _mtime(SERIES_PATH), _mtime(WATCHMAP_PATH), _mtime(FINAL_PATH))
        with _LOCK:
            if _CACHE["key"] == key and _CACHE["payload"] is not None:
                return _CACHE["payload"]
        pool, picks = _load_pool(pool_date) if pool_date else ({}, [])
        watchmap = {}
        try:
            with open(WATCHMAP_PATH, encoding="utf-8") as f:
                watchmap = json.load(f)
        except Exception:  # noqa: BLE001
            pass
        series = _series()
        data = compute(picks, series, watchmap, final_round=_final_round(series))
        if data.get("ok"):
            data["pool_date"] = pool_date
            data["env_cn"] = pool.get("env_cn")
        with _LOCK:
            _CACHE["key"], _CACHE["payload"] = key, data
        return data
    except Exception as e:  # noqa: BLE001 - 执行卡失败不影响竞价页其余面板
        return {"ok": False, "note": f"计算失败: {type(e).__name__}: {str(e)[:120]}", "rows": []}
