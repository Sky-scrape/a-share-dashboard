"""Full-round runner: frozen rule version over the entire validation interval."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .data import build_store, SEL_START, SEL_END
from .precompute import precompute
from .select import select_limit_up, select_non_lu, in_pick_universe
from .validate import validate_pick
from . import rules as R


def run_round(version: str, out_dir: Path, store=None, pre=None) -> dict:
    cfg = R.VERSIONS[version]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if store is None:
        store = build_store()
    if pre is None:
        pre = precompute(store, cfg)

    ms = pre["ms"].set_index("date")
    si = pre["si"]
    si_by_date = {d: g for d, g in si.groupby("date")}

    tdays = [d for d in store.trade_dates if SEL_START <= d <= SEL_END]
    picks_rows, val_rows, daily_notes = [], [], []

    for T in tdays:
        T1 = store.next_trade_date(T)
        if T1 is None:
            continue
        mrow = ms.loc[T] if T in ms.index else None
        regime = mrow["regime"] if mrow is not None else "normal"
        qlu, qnlu = cfg["quotas"][regime]

        lu_picks = select_limit_up(pre, store, T, qlu, cfg["lu_group"])
        si_day = si_by_date.get(T)
        nlu_picks = select_non_lu(pre, store, si_day if si_day is not None else si.iloc[0:0],
                                  mrow, T, qnlu, cfg["nlu_group"])

        # scope guard: final picks are SH/SZ main board only; research layers may use full market
        for p, gcfg in [(p, cfg["lu_group"]) for p in lu_picks] + \
                       [(p, cfg["nlu_group"]) for p in nlu_picks]:
            assert in_pick_universe(p["thscode"], gcfg), f"选股范围违规: {p['thscode']}"

        note = {"T": T, "regime": regime,
                "lu_cnt": int(mrow["lu_cnt"]) if mrow is not None else None,
                "adv_ratio": float(mrow["adv_ratio"]) if mrow is not None else None,
                "n_lu_picks": len(lu_picks), "n_nlu_picks": len(nlu_picks),
                "note": ""}
        if len(lu_picks) < 2 and regime in ("aggressive", "normal"):
            note["note"] += f"涨停组不足{2}只(候选不足或得分低于阈值); "
        if len(nlu_picks) < 2 and regime in ("aggressive", "normal"):
            note["note"] += f"非涨停组不足{2}只; "
        if regime == "freeze":
            note["note"] += "市场冰点, 降为观察模式; "
        daily_notes.append(note)

        for p in lu_picks + nlu_picks:
            picks_rows.append(dict(T=T, T1=T1, regime=regime, **p))
            v = validate_pick(p, store, pre, T, T1, cfg)
            if v is None:
                val_rows.append(dict(T=T, T1=T1, group=p["group"], thscode=p["thscode"],
                                     name=p["name"], industry=p["industry"],
                                     pick_score=p["score"], attribution="停牌/数据缺失",
                                     win=None, close_ret=np.nan))
            else:
                v["regime"] = regime
                val_rows.append(v)

    picks = pd.DataFrame(picks_rows)
    vals = pd.DataFrame(val_rows)
    notes = pd.DataFrame(daily_notes)

    picks.to_csv(out_dir / "picks.csv", index=False, encoding="utf-8-sig")
    vals.to_csv(out_dir / "validation.csv", index=False, encoding="utf-8-sig")
    notes.to_csv(out_dir / "daily_notes.csv", index=False, encoding="utf-8-sig")

    stats = summarize(vals, notes, version)
    (out_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    write_report(out_dir, stats, vals, notes, version)
    return stats


def _grp_stats(v: pd.DataFrame) -> dict:
    v = v[v["win"].notna()] if "win" in v else v
    if v.empty:
        return dict(n=0)
    cr = v["close_ret"].dropna()
    wins, losses = cr[cr > 0], cr[cr <= 0]
    return dict(
        n=int(len(v)),
        win_rate=round(float((cr > 0).mean() * 100), 2),
        avg_close=round(float(cr.mean()), 3),
        median_close=round(float(cr.median()), 3),
        avg_high=round(float(v["high_ret"].mean()), 3),
        avg_low=round(float(v["low_ret"].mean()), 3),
        avg_open=round(float(v["open_ret"].mean()), 3),
        pl_ratio=round(float(wins.mean() / abs(losses.mean())), 3) if len(wins) and len(losses) else None,
        buy_trigger_rate=round(float(v["buy_triggered"].mean() * 100), 2),
        risk_hit_rate=round(float(v["risk_hit"].mean() * 100), 2),
        logic_realized_rate=round(float(v["logic_realized"].mean() * 100), 2),
        strong_sector_rate=round(float(v["strong_sector"].mean() * 100), 2),
        strong_concept_rate=(round(float(v["strong_concept"].mean() * 100), 2)
                             if "strong_concept" in v and v["strong_concept"].notna().any() else None),
        total_ret=round(float(cr.sum()), 2),
        avg_result_score=round(float(v["result_score"].mean()), 1),
        avg_logic_score=round(float(v["logic_score"].mean()), 1),
        avg_composite=round(float(v["composite"].mean()), 1),
        rating_dist={k: int((v["rating"] == k).sum()) for k in "ABCD"},
    )


def summarize(vals: pd.DataFrame, notes: pd.DataFrame, version: str) -> dict:
    stats = dict(version=version, days=int(len(notes)),
                 total_picks=int(len(vals)))
    if vals.empty:
        return stats
    v = vals[vals["win"].notna()]
    stats["missing"] = int(len(vals) - len(v))
    stats["overall"] = _grp_stats(v)
    stats["lu"] = _grp_stats(v[v["group"] == "lu"])
    stats["nlu"] = _grp_stats(v[v["group"] == "nlu"])
    # by regime
    stats["by_regime"] = {r: _grp_stats(g) for r, g in v.groupby("regime")}
    # by month
    v2 = v.copy()
    v2["month"] = v2["T"].str[:7]
    stats["by_month"] = {m: _grp_stats(g) for m, g in v2.groupby("month")}
    # attribution
    stats["attribution"] = {
        grp: g["attribution"].value_counts().to_dict()
        for grp, g in v.groupby("group")
    }
    stats["regime_days"] = notes["regime"].value_counts().to_dict()
    # scope audit: picks must be SH/SZ only (research uses full market)
    ex = vals["thscode"].str.split(".").str[-1]
    stats["scope"] = dict(pick_exchanges=ex.value_counts().to_dict(),
                          bj_picks=int((ex == "BJ").sum()))
    return stats


def write_report(out_dir: Path, stats: dict, vals: pd.DataFrame, notes: pd.DataFrame, version: str):
    lines = [f"# 轮次报告 {version}", ""]
    o = stats.get("overall", {})
    lines.append(f"- 交易日: {stats['days']}  总选股: {stats['total_picks']}  停牌/缺失: {stats.get('missing', 0)}")
    lines.append(f"- 整体: 胜率 {o.get('win_rate')}% 平均次日 {o.get('avg_close')}% 盈亏比 {o.get('pl_ratio')} "
                 f"平均最大回撤 {o.get('avg_low')}% 逻辑兑现率 {o.get('logic_realized_rate')}%")
    for g in ("lu", "nlu"):
        s = stats.get(g, {})
        if not s.get("n"):
            continue
        lines.append(f"\n## {'涨停组' if g == 'lu' else '非涨停组'} (n={s['n']})")
        lines.append(f"- 胜率 {s['win_rate']}% | 平均次日 {s['avg_close']}% | 平均开盘 {s['avg_open']}% | 平均最高 {s['avg_high']}% | 平均最低 {s['avg_low']}%")
        lines.append(f"- 盈亏比 {s['pl_ratio']} | 买点触发率 {s['buy_trigger_rate']}% | 风险位触发率 {s['risk_hit_rate']}%")
        # 概念数据全缺时 strong_concept_rate=None：整段省略而非显示 "None%"
        _sc = f"强于概念 {s['strong_concept_rate']}% | " if s.get("strong_concept_rate") is not None else ""
        lines.append(f"- 逻辑兑现率 {s['logic_realized_rate']}% | 强于板块 {s['strong_sector_rate']}% | {_sc}累计收益(单利和) {s['total_ret']}%")
        lines.append(f"- 评级分布 {s['rating_dist']} | 结果分 {s['avg_result_score']} 逻辑分 {s['avg_logic_score']} 综合 {s['avg_composite']}")
    lines.append("\n## 按市场环境")
    for r, s in stats.get("by_regime", {}).items():
        if s.get("n"):
            lines.append(f"- {r}: n={s['n']} 胜率{s['win_rate']}% 均次{s['avg_close']}% 盈亏比{s['pl_ratio']}")
    lines.append("\n## 按月份")
    for m, s in stats.get("by_month", {}).items():
        if s.get("n"):
            lines.append(f"- {m}: n={s['n']} 胜率{s['win_rate']}% 均次{s['avg_close']}% 回撤{s['avg_low']}%")
    lines.append("\n## 归因分布")
    for g, d in stats.get("attribution", {}).items():
        lines.append(f"- {'涨停组' if g == 'lu' else '非涨停组'}: " +
                     ", ".join(f"{k}:{v}" for k, v in sorted(d.items(), key=lambda x: -x[1])))
    lines.append(f"\n市场环境日数: {stats.get('regime_days')}")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "V1"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("runs") / f"round_{version}"
    stats = run_round(version, out)
    print(json.dumps(stats.get("overall", {}), ensure_ascii=False))


if __name__ == "__main__":
    main()
