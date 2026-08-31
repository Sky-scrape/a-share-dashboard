# -*- coding: utf-8 -*-
"""量化平台板块 · 引擎接线 API（server 进程内直跑的快操作 + 长任务管理）。

设计边界：
- 本项目 HTTP 层只做参数校验与序列化，全部量化语义都在 quant_sim 引擎内（与
  quant/ CLI 共用同一引擎，测试保底一致）。
- 回测/元信息/策略库读写是秒级操作 → 进程内同步；
  选股/信号/网格这类分钟级、可能碰网络的任务 → run_job.py 子进程 + 任务文件，
  与本项目「抓取后台化」的惯例一致。
- 一切落盘只发生在 quant/ 与 data/quant/ 内，不写外部路径。
"""
import itertools
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid

from quant_config import QUANT_ROOT, JOBS_DIR, JOBS_KEEP

_lock = threading.RLock()   # 2026-09-04 加锁：server 为多线程 HTTP，缓存/任务表此前无锁
_ready = False
_meta_cache = {"ts": 0.0, "data": None}
_META_TTL = 60.0
_jobs = {}   # job_id -> Popen（本进程生命周期内的句柄，跨重启靠任务文件）


# ---------------- 引擎懒加载（server 启动不背 pandas 的锅） ----------------

def _import_engine():
    global _ready
    if not _ready:
        if QUANT_ROOT not in sys.path:
            sys.path.insert(0, QUANT_ROOT)
        _ready = True
    import quant_sim                                     # noqa: F401
    from quant_sim import BacktestConfig, run_backtest   # noqa: F401
    from quant_sim.data import loader as _loader         # noqa: F401
    return True


def engine_ready():
    return os.path.isdir(os.path.join(QUANT_ROOT, "quant_sim"))


# ---------------- JSON 消毒 ----------------

def _jd(v):
    """任意标量 → JSON 可序列化（NaN/Inf→None，numpy/Timestamp→原生）。"""
    if v is None:
        return None
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    except TypeError:
        pass
    if hasattr(v, "item"):                 # numpy 标量
        try:
            v = v.item()
        except Exception:
            pass
    if hasattr(v, "isoformat") and not isinstance(v, str):   # date/datetime/Timestamp
        try:
            return v.isoformat()[:19] if hasattr(v, "hour") else v.isoformat()[:10]
        except Exception:
            return str(v)
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _df_records(df, cap=800):
    import pandas as pd
    if df is None or (hasattr(df, "empty") and df.empty):
        return []
    d = df.copy()
    if isinstance(d.index, pd.DatetimeIndex):
        d.insert(0, "date", d.index.strftime("%Y-%m-%d"))
    recs = d.to_dict("records")
    return [{k: _jd(v) for k, v in r.items()} for r in recs[:cap]]


# ---------------- meta：前端表单的单一事实源 ----------------

_FAMILIES = [
    {"key": "dual_ma", "label": "双均线趋势（单标的）", "params": [
        {"k": "fast", "label": "快线 EMA/SMA 周期", "type": "int", "default": 20, "min": 3, "max": 120},
        {"k": "slow", "label": "慢线周期", "type": "int", "default": 60, "min": 5, "max": 250},
        {"k": "target_weight", "label": "目标仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05},
        {"k": "atr_stop", "label": "ATR 移动止损倍数（0=关）", "type": "float", "default": 0, "min": 0, "max": 10, "step": 0.5},
    ]},
    {"key": "momentum", "label": "ETF 动量轮动（多标的）", "params": [
        {"k": "lookback", "label": "动量回看（日）", "type": "int", "default": 60, "min": 20, "max": 250},
        {"k": "top_n", "label": "持有前 N 名", "type": "int", "default": 2, "min": 1, "max": 5},
        {"k": "monthly", "label": "月度调仓（否则每日）", "type": "bool", "default": True},
        {"k": "abs_momentum", "label": "绝对动量过滤（负动量不持有）", "type": "bool", "default": True},
        {"k": "weight_per_slot", "label": "每槽仓位", "type": "float", "default": 0.48, "min": 0.05, "max": 1.0, "step": 0.02},
        {"k": "skip_recent", "label": "跳过最近 N 日（防短期反转）", "type": "int", "default": 0, "min": 0, "max": 20},
    ]},
    {"key": "meanrev", "label": "均值回归分批抄底（单标的）", "params": [
        {"k": "window", "label": "均线窗口", "type": "int", "default": 20, "min": 5, "max": 60},
        {"k": "num_std", "label": "买入带（N 倍标准差）", "type": "float", "default": 2.0, "min": 0.5, "max": 4, "step": 0.25},
        {"k": "max_batches", "label": "最大分批数", "type": "int", "default": 4, "min": 1, "max": 6},
        {"k": "weight_per_batch", "label": "每批仓位", "type": "float", "default": 0.24, "min": 0.05, "max": 1.0, "step": 0.02},
        {"k": "exit_to_mid", "label": "回到均线即离场", "type": "bool", "default": True},
    ]},
]

_BOARDS = ["主板", "创业板", "科创板", "北交所"]


def meta(force=False):
    now = time.time()
    with _lock:
        if not force and _meta_cache["data"] and now - _meta_cache["ts"] < _META_TTL:
            return _meta_cache["data"]
    _import_engine()
    from quant_sim.strategies import rule_based as rb
    from quant_sim.research import screener as sc
    conds = {}
    for key, c in sc.CONDITIONS.items():
        conds[key] = {k: c[k] for k in ("kind", "label", "group", "unit", "step", "dec", "valuation")
                      if k in c}
    daily_dir = os.path.join(QUANT_ROOT, "data", "cn_a", "daily")
    local_syms = []
    if os.path.isdir(daily_dir):
        local_syms = sorted({f.split(".")[0] for f in os.listdir(daily_dir)
                             if f.endswith((".parquet", ".csv")) and not f.startswith("_")})
    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quant_root": QUANT_ROOT,
        "families": _FAMILIES,
        "rule": {
            "fields": rb.FIELDS, "ops": rb.OPS, "indicators": rb.INDICATORS,
            "spec_keys": list(rb.SPEC_KEYS),
        },
        "screener": {
            "templates": {k: {"bools": list(v.get("bools") or []),
                              "ranges": {str(a): [b, c] for a, (b, c) in (v.get("ranges") or {}).items()},
                              "note": v.get("note", "")}
                         for k, v in sc.TEMPLATES.items()},
            "universes": list(sc.UNIVERSES),
            "conditions": conds,
            "display_cols": sc.DISPLAY_COLS,
            "boards": _BOARDS,
        },
        "local_symbols": local_syms,
        "data_freshness": data_freshness(),
        "execution_opts": [
            {"v": "next_open", "label": "T+1 开盘撮合（推荐）"},
            {"v": "close", "label": "T 日收盘成交（乐观口径）"},
        ],
        "slip_opts": [
            {"v": "spread", "label": "价差模型 min((高-低)×k, 价×r)"},
            {"v": "percent", "label": "固定百分比"},
            {"v": "tick", "label": "N 个最小报价单位"},
            {"v": "none", "label": "无滑点"},
        ],
    }
    with _lock:
        _meta_cache["data"] = data
        _meta_cache["ts"] = now
    return data


# ---------------- 数据加载 ----------------

_DATA_DIR_REL = os.path.join("data", "cn_a", "daily")


def load_panel_for_api(codes, start=None, end=None, adjust="qfq", mode="auto"):
    """mode=local 只读本地缓存；mode=auto 缺票时走 hithink 自动导出（个股本地库/ETF远端）。"""
    _import_engine()
    from quant_sim.data import loader

    daily = os.path.join(QUANT_ROOT, _DATA_DIR_REL)
    if mode == "local":
        panel = loader.load_panel(daily, symbols=list(codes),
                                  date_range=(start, end) if (start and end) else None,
                                  expect_adjust=adjust)
        missing = [c for c in codes if c not in panel.symbols]
        if missing:
            raise ValueError(f"本地无这些标的的数据：{missing}；换「自动取数」或先用 CLI 导出（python -m quant_sim.data.hithink export）")
        return panel
    from quant_sim.data.hithink import load_panel as ht_load
    return ht_load(list(codes), start=start or "2018-01-01", end=end, adjust=adjust, auto=True)


def panel_warnings(panel):
    md = getattr(panel, "metadata", {}) or {}
    out = []
    if md.get("manifest_mismatch"):
        out.append("⚠️ 数据口径与所选复权不一致：" + json.dumps(md["manifest_mismatch"], ensure_ascii=False)[:200])
    if md.get("manifest_unknown"):
        out.append("⚠️ 以下序列口径未知（无 manifest）：" + ", ".join(md["manifest_unknown"][:8]))
    if md.get("outlier_stats"):
        out.append("⚠️ 检测到疑似厂商错价点：" + json.dumps(md["outlier_stats"], ensure_ascii=False)[:200])
    return out


# ---------------- 策略构造 ----------------

def build_strategy_from_desc(desc, codes, allow_exec=False):
    """desc: {kind: builtin|rule|code, ...}；codes 为默认标的池。"""
    _import_engine()
    kind = desc.get("kind", "builtin")
    if kind == "builtin":
        from quant_sim.strategies.moving_average import DualMAStrategy
        from quant_sim.strategies.momentum_ranking import MomentumRankingStrategy
        from quant_sim.strategies.mean_reversion import MeanReversionStrategy
        fam = desc.get("family")
        params = dict(desc.get("params") or {})
        if fam == "dual_ma":
            sym = params.pop("symbol", None) or (codes[0] if codes else None)
            if not sym:
                raise ValueError("双均线策略需要标的（标的池第一只或 params.symbol）")
            fast, slow = int(params.get("fast", 20)), int(params.get("slow", 60))
            if fast >= slow:
                raise ValueError(f"fast({fast}) 必须小于 slow({slow})")
            if not params.get("atr_stop"):
                params.pop("atr_stop", None)
            return DualMAStrategy(symbol=sym, **params)
        if fam == "momentum":
            uni = params.pop("universe", None) or codes
            if len(uni) < 2:
                raise ValueError("动量轮动至少需要 2 只标的池")
            return MomentumRankingStrategy(universe=list(uni), **params)
        if fam == "meanrev":
            sym = params.pop("symbol", None) or (codes[0] if codes else None)
            if not sym:
                raise ValueError("均值回归策略需要标的")
            return MeanReversionStrategy(symbol=sym, **params)
        raise ValueError(f"未知内置策略族 {fam!r}（可选 dual_ma/momentum/meanrev）")
    if kind == "rule":
        from quant_sim.strategies.rule_based import GenericRuleStrategy
        spec = dict(desc.get("spec") or {})
        spec.setdefault("symbols", list(codes))
        if not spec["symbols"]:
            raise ValueError("规则策略需要标的池")
        return GenericRuleStrategy(**spec)      # 未知 spec 键会 strict raise
    if kind == "code":
        from quant_sim.strategies.sandbox import build_user_strategy
        code = desc.get("code") or ""
        if not code.strip():
            raise ValueError("代码策略 payload 为空")
        return build_user_strategy(code)
    raise ValueError(f"未知策略类型 {kind!r}")


# ---------------- 同步回测 ----------------

_METRIC_PICK = ["回测区间", "交易日数", "初始资金", "期末权益", "累计收益率", "年化收益率",
                "年化波动率", "最大回撤", "夏普比率", "索提诺比率", "卡玛比率", "日胜率",
                "交易次数", "胜率", "盈亏比", "平均持仓天数", "总手续费", "滑点成本",
                "年化换手率(双边)", "基准年化收益", "年化超额收益", "信息比率", "Beta"]


def run_backtest_api(req):
    """req: {strategy:{kind,...}, codes[], start,end, cash, benchmark, execution,
    commission_wp, min_comm, stamp_wp, slip_model, slip_value, participation,
    max_dd_halt, block_limit, liquidate_on_end, warmup_bars, adjust, data_mode,
    title?, log(bool 是否追加研究流水，默认 true)}"""
    _import_engine()
    from quant_sim import run_backtest
    from quant_sim.research.runner import make_backtest_config

    codes = [str(c).strip() for c in (req.get("codes") or []) if str(c).strip()]
    if not codes:
        raise ValueError("标的池为空")
    if len(codes) > 30:
        raise ValueError("同步回测限 30 只标的；更大规模请走「参数研究」后台任务")
    benchmark = (req.get("benchmark") or "").strip() or None
    load_codes = list(dict.fromkeys(codes + ([benchmark] if benchmark else [])))
    panel = load_panel_for_api(load_codes, req.get("start"), req.get("end"),
                               req.get("adjust", "qfq"), req.get("data_mode", "auto"))
    strategy = build_strategy_from_desc(req.get("strategy") or {}, panel.symbols)
    cfg = make_backtest_config(
        start=req.get("start"), end=req.get("end"),
        cash=float(req.get("cash") or 1_000_000),
        execution=req.get("execution", "next_open"),
        commission=float(req.get("commission_wp", 2.5)) / 10000.0,
        min_comm=float(req.get("min_comm", 5.0)),
        stamp=float(req.get("stamp_wp", 5.0)) / 10000.0,
        slip_model=req.get("slip_model", "spread"),
        slip_value=float(req.get("slip_value", 5e-4)),
        participation=float(req.get("participation", 0.05)),
        max_dd_halt=float(req.get("max_dd_halt", 0.0)),
        benchmark=benchmark if benchmark in panel.symbols else None,
        block_limit=bool(req.get("block_limit", True)),
        liquidate_on_end=bool(req.get("liquidate_on_end", True)),
        warmup_bars=int(req.get("warmup_bars", 0)),
    )
    r = run_backtest(strategy, panel, cfg)

    eq = r.equity
    dates = [d.strftime("%Y-%m-%d") for d in eq.index]
    resp = {
        "metrics": {k: _jd(r.metrics.get(k)) for k in _METRIC_PICK if k in r.metrics},
        "all_metrics": {k: _jd(v) for k, v in r.metrics.items()},
        "curve": {
            "dates": dates,
            "equity": [_jd(x) for x in eq.values],
            "cash": [_jd(x) for x in r.cash.values],
            "holdings": [_jd(x) for x in r.holdings_value.values],
            "drawdown": [_jd(x) for x in r.drawdown.values],
            "benchmark": [_jd(x) for x in r.benchmark.values] if r.benchmark is not None else None,
            "weights": {s: [_jd(x) for x in r.weights[s].values] for s in r.weights.columns} if not r.weights.empty else {},
        },
        "trades": _df_records(r.trades, cap=500),
        "fills_n": int(len(r.fills)),
        "risk_events": _risk_event_dicts(r.risk_events)[:200],
        "logs": (r.logs or [])[-30:],
        "force_liquidated": bool(getattr(r, "force_liquidated", False)),
        "warnings": panel_warnings(panel),
        "config": {"codes": codes, "benchmark": cfg.benchmark, "window": f"{req.get('start') or ''}~{req.get('end') or ''}",
                   "cash": cfg.initial_cash, "execution": cfg.execution,
                   "adjust": req.get("adjust", "qfq"), "data_mode": req.get("data_mode", "auto")},
    }
    if req.get("save"):
        import re as _re
        nm = str(req.get("save_name") or "").strip()
        if nm and not _re.fullmatch(r"[\w.\-一-龥]{1,40}", nm):
            raise ValueError("存档名只允许中英文/数字/下划线短横，≤40 字")
        name = nm or ("native_" + time.strftime("%Y%m%d_%H%M%S"))
        try:
            r.save(out_dir=os.path.join(QUANT_ROOT, "results"), name=name,
                   data_note="原生工作台归档")
            resp["report_name"] = name
            resp["report_url"] = "/quant-results/" + name + "_report.html"
        except Exception as e:
            resp["save_error"] = str(e)[:200]
    if req.get("log", True):
        _append_backtest_log(req, resp)
    return resp


def _append_backtest_log(req, resp):
    """研究流水：与 CLI 同一文件（results/backtest_log.jsonl），原生页跑的回测也被追踪。"""
    try:
        m = resp["metrics"]
        s = req.get("strategy") or {}
        strat_name = {"builtin": f"{s.get('family')}", "rule": "GenericRuleStrategy",
                      "code": "UserCodeStrategy"}.get(s.get("kind", "builtin"), "?")
        if s.get("kind") == "rule":
            strat_name = "GenericRuleStrategy"
        row = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "title": (req.get("title") or "").strip()[:40] or f"原生回测：{(req.get('codes') or ['?'])[0]}",
            "strategy": strat_name,
            "symbols": req.get("codes") or [],
            "window": m.get("回测区间", ""),
            "cum_ret": m.get("累计收益率"), "cagr": m.get("年化收益率"),
            "max_dd": m.get("最大回撤"), "sharpe": m.get("夏普比率"),
            "trades": m.get("交易次数"),
        }
        import re as _re2
        store = _re2.sub(r"[^\w一-鿿-]+", "_", str(req.get("save_name") or "").strip())[:40]
        if store:
            # 归集键：与引擎 store._safe() 同规则清洗（黒点等标点→下划线），保证与存档名对得上
            row["store"] = store
        fp = os.path.join(QUANT_ROOT, "results", "backtest_log.jsonl")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass   # 流水记录失败不影响回测结果返回


# ---------------- 数据新鲜度（概览胶囊，10 分钟缓存） ----------------

_fresh_cache = {"ts": 0.0, "data": None}
_FRESH_TTL = 600.0


def data_freshness(force=False):
    """扫本地日线缓存每个标的的最后一个交易日 → {latest, symbols, n, oldest[]}。
    只读 parquet 的 date 列（95 文件约亚秒级），失败的文件跳过。"""
    now = time.time()
    if not force and _fresh_cache["data"] and now - _fresh_cache["ts"] < _FRESH_TTL:
        return _fresh_cache["data"]
    import glob as _glob
    daily = os.path.join(QUANT_ROOT, _DATA_DIR_REL)
    per = {}
    try:
        import pyarrow.parquet as pq
        for fp in _glob.glob(os.path.join(daily, "*.parquet")):
            sym = os.path.basename(fp).split(".")[0]
            try:
                pf = pq.ParquetFile(fp)
                if "date" not in pf.schema_arrow.names:
                    continue
                col = pf.read(columns=["date"]).column("date").to_pylist()
                vals = [str(v)[:10] for v in col if v is not None]
                if vals:
                    per[sym] = max(vals)
            except Exception:
                continue
    except Exception:
        pass
    latest = max(per.values()) if per else None
    data = {
        "latest": latest,
        "symbols": len(per),
        "n": len(per),
        "oldest": [{"symbol": s, "last": d} for s, d in sorted(per.items(), key=lambda kv: kv[1])
                   if latest and d < latest][:6],
    }
    _fresh_cache["data"] = data
    _fresh_cache["ts"] = now
    return data


def _risk_event_dicts(events):
    """RiskEvent dataclass → 结构化 dict（前端在净值图上打标记用）。"""
    out = []
    for e in (events or []):
        if isinstance(e, dict):
            d = e
        else:
            d = {"date": getattr(e, "date", None), "kind": getattr(e, "kind", ""),
                 "symbol": getattr(e, "symbol", None), "message": getattr(e, "message", ""),
                 "action": getattr(e, "action", "")}
        out.append({k: _jd(v) for k, v in d.items()})
    return out


def compare_api(req):
    """多策略/多参数同窗对比：items=[{label, req:<回测请求>}], 2~4 项串行跑（不写研究流水）。
    返回各自归一净值（≤240 点）+ 指标子集 + 可选基准曲线；单项失败进 failed 不阻断。"""
    items = req.get("items") or []
    if not (2 <= len(items) <= 4):
        raise ValueError("对比需要 2~4 项")
    series, failed = [], []
    bench = None
    for it in items:
        label = str(it.get("label") or "?")[:24]
        sub = dict(it.get("req") or {})
        sub["log"] = False
        try:
            resp = run_backtest_api(sub)
        except Exception as e:
            failed.append({"label": label, "error": str(e)[:200]})
            continue
        c = resp["curve"]
        n = len(c["dates"])
        step = max(1, math.ceil(n / 240))
        eq0 = c["equity"][0] or 1.0
        if bench is None and c.get("benchmark"):
            bench = {"label": "基准 " + str(resp["config"].get("benchmark") or ""),
                     "dates": c["dates"][::step],
                     "norm": [_jd((x or eq0) / eq0) for x in c["benchmark"][::step]]}
        series.append({"label": label,
                       "dates": c["dates"][::step],
                       "norm": [_jd(x / eq0) for x in c["equity"][::step]],
                       "metrics": resp["metrics"]})
    if len(series) < 2:
        raise ValueError("有效对比结果不足 2 条：" + json.dumps(failed, ensure_ascii=False)[:300])
    return {"series": series, "failed": failed, "benchmark": bench}


# ---------------- 任务历史（折叠面板回看最近 30 个） ----------------

def jobs_list():
    """任务历史：读 data/quant/jobs/*.json 摘要（默认留最近 30 个，按时间倒序）。"""
    try:
        files = [os.path.join(JOBS_DIR, f) for f in os.listdir(JOBS_DIR)
                 if f.endswith(".json") and not f.endswith(".params.json")]
    except OSError:
        return {"jobs": []}
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for fp in files[:30]:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        p = d.get("params") or {}
        brief = (p.get("title") or p.get("name") or p.get("strategy_name")
                 or ",".join(str(x) for x in (p.get("codes") or p.get("universe") or [])[:4]) or "")
        out.append({"id": d.get("id"), "kind": d.get("kind"), "status": d.get("status"),
                    "started": d.get("started"), "finished": d.get("finished"),
                    "brief": str(brief)[:60],
                    "error": (str(d.get("error"))[:120] or None) if d.get("error") else None,
                    "has_result": "result" in d})
    return {"jobs": out}


# ---------------- 策略库 CRUD ----------------

def strategies_list():
    _import_engine()
    from quant_sim.strategies.store import list_strategies, strategy_description
    out = []
    for s in list_strategies():
        out.append({
            "name": s.name, "kind": s.kind, "note": s.note,
            "version": s.version, "created": s.created, "updated": s.updated,
            "data_symbols": s.data_symbols,
            "desc": strategy_description(s),
            "payload": s.payload,
        })
    return out


def strategies_save(req):
    _import_engine()
    from quant_sim.strategies.store import save_strategy
    name = (req.get("name") or "").strip()
    if not name:
        raise ValueError("策略名不能为空")
    kind = req.get("kind")
    payload = req.get("payload") or {}
    path = save_strategy(name, kind, payload, note=req.get("note") or "",
                         data_symbols=req.get("data_symbols"),
                         overwrite=bool(req.get("overwrite", True)))
    return {"status": "saved", "name": name, "file": os.path.basename(path)}


def strategies_delete(name):
    _import_engine()
    from quant_sim.strategies.store import delete_strategy
    return {"status": "deleted" if delete_strategy((name or "").strip()) else "not_found"}


def rule_preview(spec):
    """规则 spec → 等价可运行 Python 预览 + 校验问题清单（不执行）。"""
    _import_engine()
    from quant_sim.strategies.rule_based import generate_python_code, validate_spec
    problems = validate_spec(spec or {}, strict=False)
    try:
        code = generate_python_code(spec or {})
    except Exception as e:
        return {"code": "", "problems": problems + [f"导出失败：{e}"]}
    return {"code": code, "problems": problems}


def strategies_get(name):
    _import_engine()
    from quant_sim.strategies.store import load_strategy, strategy_description
    s = load_strategy((name or "").strip())
    return {"name": s.name, "kind": s.kind, "note": s.note, "version": s.version,
            "created": s.created, "updated": s.updated, "data_symbols": s.data_symbols,
            "desc": strategy_description(s), "payload": s.payload}


# ---------------- 长任务（选股 / 信号 / 网格研究） ----------------

def _prune_jobs():
    try:
        files = sorted((os.path.join(JOBS_DIR, f) for f in os.listdir(JOBS_DIR)
                        if f.endswith(".json")), key=os.path.getmtime)
        for f in files[:-JOBS_KEEP]:
            os.remove(f)
    except OSError:
        pass


def job_start(kind, params):
    if kind not in ("screener", "signals", "grid", "wf", "refresh"):
        raise ValueError(f"未知任务类型 {kind}")
    if not engine_ready():
        raise ValueError(f"量化引擎缺失：{QUANT_ROOT}")
    with _lock:
        running = [j for j, p in _jobs.items() if p.poll() is None]
        if len(running) >= 2:
            raise BlockingError("已有 2 个后台任务在跑，等一个结束再提交")
        os.makedirs(JOBS_DIR, exist_ok=True)
        job_id = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6]
        pfile = os.path.join(JOBS_DIR, f"{job_id}.params.json")
        ofile = os.path.join(JOBS_DIR, f"{job_id}.json")
        with open(pfile, "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "params": params}, f, ensure_ascii=False)
        with open(ofile, "w", encoding="utf-8") as f:
            json.dump({"id": job_id, "kind": kind, "status": "running",
                       "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "params": {k: v for k, v in params.items() if k != "code"}}, f, ensure_ascii=False)
        here = os.path.dirname(os.path.abspath(__file__))
        log = open(os.path.join(JOBS_DIR, f"{job_id}.log"), "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(here, "run_job.py"), pfile, ofile],
            cwd=here, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _jobs[job_id] = proc
        _prune_jobs()
    return {"job_id": job_id, "status": "running"}


def job_get(job_id):
    if not re_fullmatch_id(job_id):
        raise ValueError("任务 id 非法")
    ofile = os.path.join(JOBS_DIR, f"{job_id}.json")
    try:
        with open(ofile, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"id": job_id, "status": "not_found"}
    with _lock:
        proc = _jobs.get(job_id)
    if data.get("status") == "running":
        if proc is not None and proc.poll() is not None and "result" not in data and "error" not in data:
            data["status"] = "error"
            data["error"] = f"任务进程退出（code={proc.returncode}），见 jobs/{job_id}.log"
        elif proc is None:
            age = time.time() - os.path.getmtime(ofile) if os.path.isfile(ofile) else 1e9
            if age > 3600:
                data["status"] = "unknown"
                data["error"] = "任务文件超过 1 小时未更新且无进程句柄（可能随旧服务一起被杀）"
    log_fp = os.path.join(JOBS_DIR, f"{job_id}.log")
    if os.path.isfile(log_fp):
        try:
            with open(log_fp, encoding="utf-8", errors="replace") as f:
                data["log_tail"] = "".join(f.readlines()[-15:])[-2000:]
        except Exception:
            pass
    return data


def re_fullmatch_id(s):
    import re
    return bool(re.fullmatch(r"\d{6}-[0-9a-f]{6}", s or ""))


class BlockingError(ValueError):
    pass
