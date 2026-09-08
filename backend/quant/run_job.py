# -*- coding: utf-8 -*-
"""量化板块长任务子进程：python run_job.py <params.json> <out.json>

kind:
  screener  全市场条件选股（本地因子缓存 → 可选远端估值精筛）
  signals   存档策略的明日开盘执行清单（hithink 自动取数，网络任务）
  grid      参数网格研究（同标的池多参数批量回测 + 排名）
  wf        Walk-Forward 滚动样本外验证（网格选参 → 紧邻 test 窗验证 → OOS 拼接）
  refresh   本地日线缓存全量重导（引擎 export 为整文件重写；个股本地库/ETF远端自动分流）

结果整体覆写 out.json；异常也落盘为 status=error，前端轮询可见。
"""
import itertools
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import quant_api   # noqa: E402


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_screener(p, qa):
    quant_api._import_engine()
    from quant_sim.research import screener as sc

    tmpl = sc.TEMPLATES.get(p.get("template") or "") or {}
    bools = list(p.get("bools") or tmpl.get("bools") or [])
    ranges = {k: tuple(v) for k, v in (p.get("ranges") or tmpl.get("ranges") or {}).items()
              if v and (v[0] is not None or v[1] is not None)}
    uni_name = p.get("universe") or "全市场"
    if p.get("custom_codes"):
        universe = [c.strip()[:6] for c in str(p["custom_codes"]).replace(",", " ").split() if c.strip()]
    elif uni_name == "全市场":
        universe = None
    else:
        universe = sc.get_universe(uni_name)
    factors = sc.build_factors(days=int(p.get("days", 320)), refresh=bool(p.get("refresh")))
    res = sc.screen(factors, universe=universe,
                    boards=p.get("boards") or None,
                    bools=bools, ranges=ranges,
                    sort_by=p.get("sort", "amt_20"),
                    ascending=bool(p.get("ascending", False)),
                    limit=int(p.get("limit", 100)),
                    enrich=bool(p.get("enrich", False)))
    table = res.get("table")
    return {
        "table": qa._df_records(table, cap=400),
        "columns": list(table.columns) if table is not None else [],
        "display_cols": sc.DISPLAY_COLS,
        "steps": {k: _jd(v) for k, v in (res.get("steps") or {}).items()},
        "warnings": res.get("warnings") or [],
        "asof": _jd(res.get("asof")),
        "template_note": tmpl.get("note", ""),
    }


def run_signals(p, qa):
    quant_api._import_engine()
    from quant_sim.tools.signals import run_signal_for_name
    codes = p.get("codes") or None
    report = run_signal_for_name(
        p["name"],
        codes=[str(c).strip() for c in codes] if codes else None,
        start=p.get("start", "2021-09-01"),
        initial_cash=float(p.get("cash", 1_000_000)),
        allow_exec=bool(p.get("allow_exec")),
    )
    return {"report": report, "name": p["name"]}


def run_grid(p, qa):
    quant_api._import_engine()
    from quant_sim.research import runner

    codes = [str(c).strip() for c in (p.get("codes") or []) if str(c).strip()]
    if not codes:
        raise ValueError("网格研究需要标的池")
    benchmark = (p.get("benchmark") or "").strip() or None
    load_codes = list(dict.fromkeys(codes + ([benchmark] if benchmark else [])))
    panel = qa.load_panel_for_api(load_codes, p.get("start"), p.get("end"),
                                  p.get("adjust", "qfq"), p.get("data_mode", "auto"))
    grids = {}
    for k, v in (p.get("grids") or {}).items():
        vals = v if isinstance(v, list) else [x for x in str(v).replace("，", ",").split(",") if x.strip()]
        parsed = []
        for x in vals:
            try:
                n = float(x)
                parsed.append(int(n) if n == int(n) else n)
            except ValueError:
                parsed.append(x.strip())
        grids[k] = parsed
    if not grids:
        raise ValueError("网格为空：至少给一个参数的候选值列表")
    keys = list(grids)
    variants = [dict(zip(keys, combo)) for combo in itertools.product(*(grids[k] for k in keys))]
    if len(variants) > 240:
        raise ValueError(f"组合数 {len(variants)} > 240，请收缩网格")
    base_desc = dict(p.get("strategy") or {})
    fixed = dict(base_desc.get("params") or {})

    def build(kw):
        desc = {"kind": base_desc.get("kind", "builtin"), "family": base_desc.get("family")}
        desc["params"] = {**fixed, **kw}
        if base_desc.get("kind") == "rule":
            desc = {"kind": "rule", "spec": {**(base_desc.get("spec") or {}), **kw}}
        return qa.build_strategy_from_desc(desc, panel.symbols)

    cfg = runner.make_backtest_config(
        start=p.get("start"), end=p.get("end"),
        cash=float(p.get("cash", 1_000_000)),
        execution=p.get("execution", "next_open"),
        commission=float(p.get("commission_wp", 2.5)) / 10000.0,
        min_comm=float(p.get("min_comm", 5.0)),
        stamp=float(p.get("stamp_wp", 5.0)) / 10000.0,
        slip_model=p.get("slip_model", "spread"), slip_value=float(p.get("slip_value", 5e-4)),
        participation=float(p.get("participation", 0.05)),
        benchmark=benchmark if benchmark in panel.symbols else None,
        warmup_bars=int(p.get("warmup_bars", 60)),
    )
    df, _ = runner.run_variants(variants, build, panel, cfg, lite=True)
    ranked = runner.rank_variants(df, keys, rank_by=p.get("rank_by", "夏普比率"))
    return {
        "grid_keys": keys,
        "n_variants": len(variants),
        "rows": qa._df_records(ranked, cap=300),
        "columns": list(ranked.columns),
        "rank_by": p.get("rank_by", "夏普比率"),
        "warnings": qa.panel_warnings(panel),
    }


def run_wf(p, qa):
    """滚动 WF：与 grid 同一 desc 合并约定；每折训练窗网格选参后立刻在紧邻 test 窗验证。"""
    quant_api._import_engine()
    from quant_sim.research.runner import make_backtest_config
    from quant_sim.research.grid_walkforward import walk_forward

    codes = [str(c).strip() for c in (p.get("codes") or []) if str(c).strip()]
    if not codes:
        raise ValueError("WF 研究需要标的池")
    benchmark = (p.get("benchmark") or "").strip() or None
    load_codes = list(dict.fromkeys(codes + ([benchmark] if benchmark else [])))
    panel = qa.load_panel_for_api(load_codes, p.get("start"), p.get("end"),
                                  p.get("adjust", "qfq"), p.get("data_mode", "auto"))
    grids = {}
    for k, v in (p.get("grids") or {}).items():
        vals = v if isinstance(v, list) else [x for x in str(v).replace("，", ",").split(",") if x.strip()]
        parsed = []
        for x in vals:
            try:
                n = float(x)
                parsed.append(int(n) if n == int(n) else n)
            except ValueError:
                parsed.append(x.strip())
        grids[k] = parsed
    if not grids:
        raise ValueError("网格为空：WF 每折都要跑网格，至少给一个参数的候选值列表")
    n_combo = 1
    for v in grids.values():
        n_combo *= len(v)
    if n_combo > 60:
        raise ValueError(f"WF 网格组合数 {n_combo} > 60（每折都要全跑一遍），请收缩候选")
    base_desc = dict(p.get("strategy") or {})
    fixed = dict(base_desc.get("params") or {})

    def build(**kw):   # walk_forward/grid_search 以 factory(**combo) kwargs 约定调用
        desc = {"kind": base_desc.get("kind", "builtin"), "family": base_desc.get("family")}
        desc["params"] = {**fixed, **kw}
        if base_desc.get("kind") == "rule":
            desc = {"kind": "rule", "spec": {**(base_desc.get("spec") or {}), **kw}}
        return qa.build_strategy_from_desc(desc, panel.symbols)

    cfg = make_backtest_config(
        start=p.get("start"), end=p.get("end"),
        cash=float(p.get("cash", 1_000_000)),
        execution=p.get("execution", "next_open"),
        commission=float(p.get("commission_wp", 2.5)) / 10000.0,
        min_comm=float(p.get("min_comm", 5.0)),
        stamp=float(p.get("stamp_wp", 5.0)) / 10000.0,
        slip_model=p.get("slip_model", "spread"), slip_value=float(p.get("slip_value", 5e-4)),
        participation=float(p.get("participation", 0.05)),
        benchmark=benchmark if benchmark in panel.symbols else None,
    )
    res = walk_forward(build, grids, panel, cfg,
                       train_days=int(p.get("train_days", 244)),
                       test_days=int(p.get("test_days", 63)),
                       rank_by=p.get("rank_by", "夏普比率"))
    oos = res.oos_equity
    step = max(1, len(oos) // 240) if len(oos) else 1
    return {
        "folds": qa._df_records(res.folds, cap=40),
        "oos_curve": {"dates": [d.strftime("%Y-%m-%d") for d in oos.index][::step],
                      "equity": [qa._jd(x) for x in oos.values][::step]},
        "oos_metrics": {k: qa._jd(v) for k, v in (res.oos_metrics or {}).items()},
        "overfit_ratio": qa._jd(res.overfit_ratio),
        "rank_by": res.rank_by,
        "warmup_days": res.warmup_days,
        "verdict": {k: qa._jd(v) for k, v in (res.verdict or {}).items()},
        "param_stability": [{kk: qa._jd(vv) for kk, vv in row.items()} for row in (res.param_stability or [])],
        "warnings": qa.panel_warnings(panel),
    }


def run_refresh(p, qa):
    """本地缓存刷新：symbols 缺省 = data/cn_a/daily 全池。
    ⚠️ 引擎 export 是整文件重写不是增量合并——默认从 2018-01-01 全量重导（与引擎 CLI 同约定），
    绝不能只导最近窗口，否则会把多年缓存截断成几十行。"""
    quant_api._import_engine()
    import glob
    from quant_sim.data.hithink import export_any

    daily = os.path.join(qa.QUANT_ROOT, "data", "cn_a", "daily")
    symbols = [str(c).strip() for c in (p.get("symbols") or []) if str(c).strip()]
    if not symbols:
        symbols = sorted({os.path.basename(f).split(".")[0]
                          for f in glob.glob(os.path.join(daily, "*.parquet"))})
    if not symbols:
        raise ValueError("本地缓存为空且未指定 symbols，无法增量刷新；请先用 CLI 导出首批数据")
    before = qa.data_freshness(force=True)
    start = p.get("start") or "2018-01-01"
    written = export_any(symbols, start=start, end=p.get("end"),
                         adjust=p.get("adjust", "qfq"), out_dir=daily)
    after = qa.data_freshness(force=True)
    return {"symbols_in": len(symbols), "written": len(written), "start": start,
            "before": before.get("latest"), "after": after.get("latest"),
            "note": "个股走本地 hithink DuckDB（秒级）；ETF/指数走远端 fund/index.history"}

class _JD:
    @staticmethod
    def __call__(v):
        try:
            if v != v or v in (float("inf"), float("-inf")):
                return None
        except TypeError:
            pass
        if hasattr(v, "item"):
            try:
                return v.item()
            except Exception:
                pass
        return v if isinstance(v, (str, int, float, bool, list, dict, type(None))) else str(v)


_jd = _JD()


def main():
    pfile, ofile = sys.argv[1], sys.argv[2]
    with open(pfile, encoding="utf-8") as f:
        doc = json.load(f)
    kind, params = doc["kind"], doc.get("params") or {}
    job_id = os.path.basename(ofile).split(".")[0]
    out = {"id": job_id, "kind": kind, "status": "running", "started": _now(), "params": params}
    try:
        handler = {"screener": run_screener, "signals": run_signals, "grid": run_grid,
                   "wf": run_wf, "refresh": run_refresh}[kind]
        result = handler(params, quant_api)
        out.update({"status": "done", "finished": _now(), "result": result})
    except Exception as e:
        out.update({"status": "error", "finished": _now(), "error": str(e)[:500],
                    "trace": traceback.format_exc()[-1500:]})
    tmp = ofile + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=_jd)
    os.replace(tmp, ofile)


if __name__ == "__main__":
    main()
