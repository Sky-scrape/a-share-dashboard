# -*- coding: utf-8 -*-
"""C12 立项 P1-b：生产 ↔ 研究引擎「影子对账」审计（样本外池日逐日重放）。

动机（阶段性总结-20260918 §四）：C7 的 20 日样本外复审统计的是生产闭环（spec_pool
build_pool 的定格池），而基线 62.09%/+2.50% 来自研究引擎（engine.select）。09-14/15
两日出现票级分歧，若不持续量化「两台机器还差多少」，到线判定的对象是模糊的。

对账三方（同池日 T）：
  1. 定格池    = data/recap/T.json 里 speculation.pool（当日生产实际产出，含 env）
  2. 生产重放  = spec_pool.build_pool(T, bundles, data)（生产代码 + 今日面板/映射重放）
  3. 引擎重放  = engine select_limit_up/select_non_lu（C7 冻结口径，同 engine run）

根因标签（对「生产重放 vs 引擎重放」的票级差）：
  env         定格 env ≠ 引擎 regime（历史定格日的当日面板滞后/修复日混合产物）
  yizi        引擎按日线 is_yizi/prev_yizi 排除而生产无当日一字排除（C13 已证字段不可近似）
  volr        生产 factors.volr=null（DuckDB 当日层缺/查询失败）而引擎有值（数据事故连锁）
  themes      当日快照 themes=0 组（修复日混合态）→ 生产独狼硬筛 tc<2 全灭（C13 发现）
  score_gap   同票入选但分差 >0.5（概念 heat 输入残差等，逐票列分数）

本脚本只读不改：不写 pool_track、不动生产代码。生产重放用今日数据，与当日定格
可能有差（正是审计对象），如实呈现两列。

用法：python strategy-iter/scripts/shadow_parity_audit.py
输出：strategy-iter/runs/shadow_parity/audit_report.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # strategy-iter/
ROOT = BASE.parent                                  # 项目根 A/
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT / "backend" / "recap"))
sys.path.insert(0, str(ROOT / "backend"))

from engine.data import build_store                 # noqa: E402
from engine.precompute import precompute            # noqa: E402
from engine import rules as R                       # noqa: E402
from engine.select import select_limit_up, select_non_lu, in_pick_universe  # noqa: E402

# ---- 生产侧（延迟导入避免 recap 包副作用先于 path 生效）----
import speculate                                    # noqa: E402
import spec_pool                                    # noqa: E402
import snapio                                       # noqa: E402
import spec_series as S                             # noqa: E402

TRACK = ROOT / "data" / "recap" / "pool_track.json"
OUT = BASE / "runs" / "shadow_parity"


def _iso(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


def _norm(c) -> str:
    return str(c).split(".")[0]


def frozen_pools() -> list[dict]:
    """pool_track rules=c7 的池日（验证日 -> 池日 e['date']），按池日升序。"""
    vals = json.loads(TRACK.read_text(encoding="utf-8")).get("validations") or {}
    out = []
    for key in sorted(vals):
        e = vals[key] or {}
        if e.get("rules") != "c7":
            continue
        out.append({"pool_day": e["date"], "valid_day": key, "picks": e.get("picks") or []})
    return out


def replay_prod(T8: str) -> dict:
    """生产代码 + 今日面板/映射 重放池日 T（不落盘，只取返回值）。"""
    snap = snapio.load(T8)
    if snap is None:
        return {"ok": False, "err": "快照缺失"}
    bundles = speculate.bundles_from_snap(snap)
    data = {"themes": ((snap.get("modules") or {}).get("speculation") or {})
            .get("data", {}).get("themes") or {"groups": []}}
    try:
        pool = spec_pool.build_pool(T8, bundles, data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"ok": True, "env": pool.get("env"),
            "picks": [(str(p.get("代码")), p.get("得分"),
                       "nlu" if p.get("类型") == "非涨停板" else "lu",
                       (p.get("factors") or {}).get("volr"))
                      for p in pool.get("picks") or []]}


def replay_engine(T: str, store, pre, cfg, ms_idx, si_by_date) -> dict:
    mrow = ms_idx.loc[T] if T in ms_idx.index else None
    regime = mrow["regime"] if mrow is not None else "normal"
    qz, qn = cfg["quotas"][regime]
    lu = select_limit_up(pre, store, T, qz, cfg["lu_group"])
    si_day = si_by_date.get(T)
    nlu = select_non_lu(pre, store, si_day if si_day is not None else pre["si"].iloc[0:0],
                        mrow, T, qn, cfg["nlu_group"])
    picks = [(_norm(p["thscode"]), round(p["score"], 1), "lu") for p in lu] + \
            [(_norm(p["thscode"]), round(p["score"], 1), "nlu") for p in nlu]
    return {"env": regime, "picks": picks}


def yizi_flags(store, pre, T: str, codes: set) -> dict:
    """引擎侧一字标记：{6位代码: 'yizi'|'prev_yizi'|None}（生产选中而引擎未选时归因用）。"""
    day = pre["pool"][pre["pool"]["date"] == T]
    out = {}
    for r in day.itertuples():
        c = _norm(r.thscode)
        if c in codes:
            if bool(getattr(r, "is_yizi", False)):
                out[c] = "yizi"
            elif bool(getattr(r, "prev_yizi", False)):
                out[c] = "prev_yizi"
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    days = frozen_pools()
    store = build_store()
    cfg = R.VERSIONS["C7"]
    pre = precompute(store, cfg)
    ms_idx = pre["ms"].set_index("date")
    si_by_date = {d: g for d, g in pre["si"].groupby("date")}

    lines = ["# 影子对账审计（生产 ↔ 引擎，C7 样本外池日）", "",
             f"- 对账池日 {len(days)} 个（{days[0]['pool_day']}..{days[-1]['pool_day']}）· "
             "只读审计，不改生产/pool_track",
             "- 三方：定格（当日生产实际）/ 生产重放（今日数据）/ 引擎重放（C7 冻结）", ""]
    n_env_gap = n_prod_gap = n_eng_gap = n_frozen_gap = 0
    total_picks = 0
    for d in days:
        T8, Tiso = d["pool_day"], _iso(d["pool_day"])
        if Tiso > store.trade_dates[-1]:
            lines.append(f"## {T8}：超出引擎数据窗口（≤{store.trade_dates[-1]}），跳过")
            continue
        eng = replay_engine(Tiso, store, pre, cfg, ms_idx, si_by_date)
        prod = replay_prod(T8)
        fz = [(_norm(p["code"]), p.get("pick_score"), p["group"]) for p in d["picks"]]
        # 生产侧一字标记（定格池因子）
        prod_codes = {c for c, _, _, _ in prod.get("picks", [])} if prod.get("ok") else {c for c, _, _ in fz}
        yz = yizi_flags(store, pre, Tiso, prod_codes)
        eng_codes = {c for c, _, _ in eng["picks"]}
        lines.append(f"## {T8}（验证日 {d['valid_day']}）")
        lines.append(f"- 环境：定格 / 引擎 = **{eng['env']}**"
                     + ("；生产重放 env 未取到" if not prod.get("ok")
                        else f"；生产重放 = {prod['env']}"
                             + (" ⚠️ 定格 env 与引擎不同" if False else "")))
        # env 对比（定格 env 从 pool_track picks 的 env 字段取）
        fz_env = (d["picks"][0].get("env") if d["picks"] else None)
        if fz_env and fz_env != eng["env"]:
            n_env_gap += 1
            lines.append(f"- ⚠️ **env 分歧**：定格池 env={fz_env}，引擎重放={eng['env']}")
        for c, s, g in fz:
            total_picks += 1
        if prod.get("ok"):
            pp = {(c, g): s for c, s, g, _ in prod["picks"]}
            ep = {(c, g): s for c, s, g in eng["picks"]}
            only_eng = {k for k in ep if k[0] not in {c for c, _ in pp}}
            only_prod = {k for k in pp if k[0] not in {c for c, _ in ep}}
            for c, s, g, volr in prod["picks"]:
                tag = "一致"
                if c not in eng_codes:
                    if yz.get(c) == "yizi":
                        tag = "**引擎一字排除（生产无该规则，C13 证不可近似）**"
                    elif yz.get(c) == "prev_yizi":
                        tag = "引擎昨日一字排除"
                    else:
                        tag = "**生产有/引擎无（候选过滤差异待查）**"
                elif volr is None:
                    tag = "⚠️ 生产量比缺失（DuckDB 当日层缺，事故连锁）"
                es = next((v for (cc, _g), v in ep.items() if cc == c), None)
                ps = s
                if tag == "一致" and es is not None and abs(float(es) - float(ps)) > 0.5:
                    tag = f"分差 {float(ps) - float(es):+.1f}"
                if tag not in ("一致",) and "分差" not in tag:
                    n_prod_gap += 1 if c not in eng_codes else 0
                lines.append(f"- 生产重放 {c} [{g}] {ps} vs 引擎 {es} · {tag}")
            for c, g in only_eng:
                s = [v for (cc, gg), v in ep.items() if cc == c]
                extra = ""
                if prod.get("ok") and not pp:
                    # 生产重放整日 0 票而引擎有票：查当日快照 themes 是否 0 组（修复日混合态）
                    try:
                        _snap = snapio.load(T8)
                        _sdata = (((_snap or {}).get("modules") or {})
                                  .get("speculation") or {}).get("data") or {}
                        _nth = len(((_sdata.get("themes") or {}).get("groups")) or [])
                        if _nth == 0:
                            extra = " · **themes=0 组→独狼硬筛全灭（修复日混合态，非规则差）**"
                    except Exception:  # noqa: BLE001
                        pass
                lines.append(f"- 引擎有/生产无 {c} [{g}] {s[0] if s else '?'} · **引擎多一票**{extra}")
            if only_eng:
                n_eng_gap += len(only_eng)
            if not only_eng and not only_prod and not any("分差" in l for l in lines[-len(pp) or 1:]):
                lines.append("- ✅ 票级与分数完全一致")
        else:
            lines.append(f"- 生产重放失败：{prod.get('err')}")
        # 定格 vs 引擎（真·样本外统计对象）
        if {c for c, _, _ in fz} != eng_codes:
            n_frozen_gap += 1
            lines.append(f"- ⚠️ **定格池 ≠ 引擎重放**（复审统计对象含此漂移）："
                         f"定格 {sorted(c for c,_,_ in fz)} vs 引擎 {sorted(eng_codes)}")
        lines.append("")
    lines += ["## 汇总", "",
              f"- 定格↔引擎 env 分歧日 {n_env_gap} · 定格票集漂移日 {n_frozen_gap}"
              f" · 生产重放↔引擎票级差 {n_prod_gap} 票 · 引擎多票 {n_eng_gap}",
              f"- 定格样本外总票 {total_picks}", ""]
    (OUT / "audit_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:12]))
    print("...\n报告已写入", OUT / "audit_report.md")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
