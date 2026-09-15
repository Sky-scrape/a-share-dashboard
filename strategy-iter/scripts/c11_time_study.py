# -*- coding: utf-8 -*-
"""C11 立项研究：LU time 权重重校准 + 门槛再平衡（rounds_log §10 唯一候选的完整评审）。

背景（§5/§10 两轮消融）：LU time 权重→0 连续两轮同向 +0.31/+0.30pp，但两轮都恰在
预注册阈线上、CI 跨零，且伴随 n 436→238（−46%）的候选面坍缩——w0 变体把权重置零
后总分上限从 100 缩到 80，门槛 70 不变，大量票被挤出候选面。**增益可能纯粹是
「缩池假象」而非 time 因子本身有害**。本轮（2026-09-15，用户批准立项）用重归一化
拆开这两个混淆因素。

变体（全部 deepcopy C7 内存构造，策略文件零改动；对照 = C7 基线）：
  ref_w0      time→0 原样复刻 §10 信号（权重和 80、门槛 70）——复现用对照组
  a_renorm    time→0 + 其余权重等比放大回总和 100（门槛 70）——**主检验**：
              候选面量级与基线相当时增益是否仍在
  b_renorm_ms a_renorm 基础上门槛在 60..76（步长 2）网格内取「候选面最接近基线」
              的一档——**稳健性检验**：门槛再平衡后增益是否仍在（全网格披露，
              杜绝挑数）
  c_half      time 权重 20→10 + 其余权重等比放大回总和 100（门槛 70）——折中档，
              若 time 含部分真实信息应在此显形

窗口：完整当前窗口（engine/data.py SEL_START..SEL_END，2026-01-02..09-10）与
C8 子窗（T ≤ 2026-09-08，即两轮消融信号所在窗口；注意消融当年 SEL_START=01-05，
本子窗多含 T=01-02 一个选股日，差异一并披露）。两窗全部指标均报告。

判定（跑数前预注册，全部满足才算通过）：
  ① a_renorm 两窗 Δ（实时均益 pp，配对日均差的逐日均值）≥ +0.30
  ② b_renorm_ms 两窗 Δ ≥ +0.30（门槛再平衡后仍在）
  ③ 两窗 equity ≥ 基线、maxDD 恶化 ≤ 1pp
  ④ 两窗前后半窗配对差同向（同正或同负）
  通过 → 建议进入采纳评审（C7 为生产冻结线，改线由用户拍板，本脚本不改任何规则）；
  ①不满足（增益消失）→ 判定「两轮消融信号为权重总量/候选面收缩假象」，C11 销项，
    time 维持 20，离场/打分主题的全部候选清单清空。
  其余组合 → 按证据方向如实记录，不达标不动规则。

统计口径：实时均益 = backtest_capital.trade_pnl realtime（执行层冻结口径，与
消融/封板研究同一实现）；Δ 的 CI = 逐日配对差 bootstrap 2000 次（seed=7）。

用法：
    python strategy-iter/scripts/c11_time_study.py
输出：<BASE>/runs/c11_window_C7/c11_time_report.md + 控制台摘要。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

import backtest_capital as bt  # noqa: E402  执行口径单一来源
from c8_factor_ablation import evaluate, run_cfg  # noqa: E402  复用主循环与评估
from engine.data import SEL_END, SEL_START, build_store  # noqa: E402
from engine.precompute import precompute  # noqa: E402
from engine import rules as R  # noqa: E402

C8_SUB_END = "2026-09-08"      # C8/C10 两轮消融信号的窗口上界
BOOT_N, BOOT_SEED = 2000, 7
TH = 0.30                      # 与消融同阈：预注册判定线 pp
C7_LU_W = dict(R.C7["lu_group"]["weights"])   # {theme5, concept15, ladder20, seal15, time20, liq10, struct15}


def renorm_without(key: str, keep_weight=None) -> dict:
    """把 key 的权重移除后，其余因子等比放大回总和 100（保留候选面量级）。
    keep_weight 不为 None 时 key 保留该权重（部分移除）。

    数学：其余因子原和 = 100 − w_orig[key]；目标 = 100 − keep；
    scale = (100 − keep) / (100 − w_orig[key])。"""
    w = dict(C7_LU_W)
    w[key] = keep_weight if keep_weight is not None else 0
    scale = (100.0 - w[key]) / (100.0 - C7_LU_W[key])
    for k in w:
        if k != key:
            w[k] = round(w[k] * scale, 4)
    total = sum(w.values())
    assert abs(total - 100.0) < 1e-3, f"renorm 后总权重 {total} != 100"
    return w


def variants():
    out = []
    w_a = renorm_without("time")
    w_c = renorm_without("time", keep_weight=10)

    def ref_w0(cfg):
        cfg["lu_group"]["weights"]["time"] = 0
    out.append(("ref_w0 time→0(复刻§10)", ref_w0))

    def a_renorm(cfg):
        cfg["lu_group"]["weights"] = dict(w_a)
    out.append(("a_renorm time→0+重归一(主检验)", a_renorm))
    return out, w_a, w_c


def daily_pnl(rows_bt):
    """逐日（T）已成交收益的均值表 {T: mean_pp}，供配对差与半窗拆分。"""
    by_t = {}
    for r in rows_bt:
        p = bt.trade_pnl(r)
        if p is not None:
            by_t.setdefault(r["T"], []).append(p * 100.0)
    return {t: float(np.mean(v)) for t, v in by_t.items()}, by_t


def paired_stats(pnl_a, pnl_b, tmax=None):
    """配对日均差（a−b，仅两边同日都有成交的日期）+ bootstrap CI + 前后半窗。"""
    ts = sorted(set(pnl_a) & set(pnl_b))
    if tmax is not None:
        ts = [t for t in ts if t <= tmax]
    if len(ts) < 10:
        return None
    d = np.array([pnl_a[t] - pnl_b[t] for t in ts])
    rng = np.random.default_rng(BOOT_SEED)
    boots = d[rng.integers(0, len(d), (BOOT_N, len(d)))].mean(axis=1)
    half = len(ts) // 2
    return {
        "days": len(ts), "mean": round(float(d.mean()), 3),
        "ci": (round(float(np.percentile(boots, 2.5)), 2),
               round(float(np.percentile(boots, 97.5)), 2)),
        "front": round(float(d[:half].mean()), 3),
        "back": round(float(d[half:].mean()), 3),
    }


def main():
    print("[1/4] build_store ...", flush=True)
    store = build_store()
    cfg7 = R.VERSIONS["C7"]
    print("[2/4] precompute (C7 regime) ...", flush=True)
    pre = precompute(store, cfg7)

    vs, w_a, w_c = variants()

    # b_renorm_ms：门槛网格（在 a_renorm 基础上）
    grid = list(range(60, 77, 2))

    def run_one(cfg):
        return evaluate(run_cfg(cfg, store, pre))

    print("[3/4] 基线 + 变体 ...", flush=True)
    base = run_one(cfg7)
    results = {}
    for name, mut in vs:
        cfg = copy.deepcopy(cfg7)
        mut(cfg)
        results[name] = run_one(cfg)
        print(f"  {name}: n={results[name]['n']} pnl={results[name]['pnl_avg']}", flush=True)

    # b 变体门槛网格（全部披露）
    grid_rows = []
    for ms in grid:
        cfg = copy.deepcopy(cfg7)
        cfg["lu_group"]["weights"] = dict(w_a)
        cfg["lu_group"]["min_score"] = float(ms)
        cfg["nlu_group"]["min_score"] = float(ms)
        r = run_one(cfg)
        grid_rows.append({"ms": ms, "r": r})
        print(f"  b_renorm_ms={ms}: n={r['n']} pnl={r['pnl_avg']}", flush=True)
    b_best = min(grid_rows, key=lambda x: abs(x["r"]["n"] - base["n"]))
    results["b_renorm_ms 门槛再平衡"] = b_best["r"]

    # c_half
    def c_half(cfg):
        cfg["lu_group"]["weights"] = dict(w_c)
    cfg = copy.deepcopy(cfg7)
    c_half(cfg)
    results["c_half time 半权10+重归一"] = run_one(cfg)
    print(f"  c_half: n={results['c_half time 半权10+重归一']['n']}", flush=True)

    # 逐日配对表（full / C8 子窗）
    pnl_base, _ = daily_pnl(base["_rows"])
    paired = {}
    for name, r in results.items():
        pnl_v, _ = daily_pnl(r["_rows"])
        paired[name] = {"full": paired_stats(pnl_v, pnl_base),
                        "c8": paired_stats(pnl_v, pnl_base, tmax=C8_SUB_END)}

    # ---- 预注册判定 ----
    def pp(x):
        return x["mean"] if x else None
    checks = {
        "① a_renorm 两窗 Δ≥+0.30": all(pp(paired["a_renorm time→0+重归一(主检验)"][w]) is not None
                                    and pp(paired["a_renorm time→0+重归一(主检验)"][w]) >= TH
                                    for w in ("full", "c8")),
        "② b_renorm_ms 两窗 Δ≥+0.30": all(pp(paired["b_renorm_ms 门槛再平衡"][w]) is not None
                                        and pp(paired["b_renorm_ms 门槛再平衡"][w]) >= TH
                                        for w in ("full", "c8")),
        "③ equity/maxDD 不恶化": all(
            results[k]["equity"] >= base["equity"]
            and results[k]["maxdd"] >= base["maxdd"] - 1.0
            for k in ("a_renorm time→0+重归一(主检验)", "b_renorm_ms 门槛再平衡")),
        "④ 前后半窗同向": all(
            (p["front"] > 0) == (p["back"] > 0)
            for nm in ("a_renorm time→0+重归一(主检验)", "b_renorm_ms 门槛再平衡")
            for p in (paired[nm]["full"], paired[nm]["c8"]) if p),
    }
    passed = all(checks.values())

    # ---- 报告 ----
    out_dir = BASE / "runs" / "c11_window_C7"
    out_dir.mkdir(parents=True, exist_ok=True)
    L = ["# C11 立项研究：LU time 权重重校准 + 门槛再平衡", "",
         f"- 日期 2026-09-15；窗口 full={SEL_START}..{SEL_END}，C8 子窗 ..{C8_SUB_END}"
         "（消融当年 SEL_START=01-05，本子窗多含 T=01-02，如实披露）",
         f"- 基线 C7：n={base['n']} · 实时均益 {base['pnl_avg']}% · equity {base['equity']} · "
         f"maxDD {base['maxdd']}%",
         "- 判定预注册（跑数前写入脚本头）：①a_renorm 两窗 Δ≥+0.30；②b_renorm_ms 两窗 Δ≥+0.30；"
         "③两窗 equity≥基线且 maxDD 恶化≤1pp；④前后半窗同向。全部满足→建议采纳评审；"
         "①不满足→「缩池假象」，C11 销项。CI=逐日配对差 bootstrap2000 次 seed=7。", "",
         "## 变体总表", "",
         "| 变体 | n(Δ) | 实时均益% | 条件均次% | 胜率% | equity | maxDD% |",
         "|---|---|---|---|---|---|---|"]
    for name in ("ref_w0 time→0(复刻§10)", "a_renorm time→0+重归一(主检验)",
                 "b_renorm_ms 门槛再平衡", "c_half time 半权10+重归一"):
        r = results[name]
        L.append(f"| {name} | {r['n']} ({r['n'] - base['n']:+d}) | {r['pnl_avg']} "
                 f"| {r['close_avg']} | {r['win']} | {r['equity']} | {r['maxdd']} |")
    L += ["", "## 配对日均差（变体−基线，pp）", "",
          "| 变体 | 窗口 | 天数 | Δ | CI95 | 前半窗 | 后半窗 |", "|---|---|---|---|---|---|---|"]
    for name, pw in paired.items():
        for w, label in (("full", "full"), ("c8", "C8子窗")):
            p = pw[w]
            if p is None:
                L.append(f"| {name} | {label} | - | 样本不足 | - | - | - |")
            else:
                L.append(f"| {name} | {label} | {p['days']} | {p['mean']:+.3f} "
                         f"| {p['ci'][0]}..{p['ci'][1]} | {p['front']:+.3f} | {p['back']:+.3f} |")
    L += ["", f"## b 变体门槛网格（a_renorm 权重，NLU/LU 同步调门槛；基线 n={base['n']}）", "",
          "| 门槛 | n | 实时均益% | equity | maxDD% |", "|---|---|---|---|---|"]
    for g in grid_rows:
        r = g["r"]
        mark = " ←最接近基线" if g is b_best else ""
        L.append(f"| {g['ms']} | {r['n']} | {r['pnl_avg']} | {r['equity']} | {r['maxdd']} |{mark}")
    L += ["", "## 预注册判定", ""]
    for k, v in checks.items():
        L.append(f"- {'✅' if v else '❌'} {k}")
    L.append("")
    if passed:
        L.append("**判定：全部通过 → 建议进入采纳评审**（C7 为生产冻结线，"
                 "改线须用户拍板后另行落地，本脚本未改任何规则文件）。")
    elif not checks["① a_renorm 两窗 Δ≥+0.30"]:
        L.append("**判定：①不满足——候选面量级持平时增益消失，§5/§10 两轮消融信号确认为"
                 "「权重总量缩水/候选面收缩」假象。C11 销项，LU time 维持 20，"
                 "消融候选清单清空（策略层证据驱动迭代到顶）。**")
    else:
        L.append("**判定：部分通过——按证据方向如实记录，不满足全部预注册条件，不动规则。**")
    L += ["", "## 分组明细", "",
          "| 变体 | LU n/exec/均益 | NLU n/exec/均益 |", "|---|---|---|"]
    for name in results:
        g = results[name]["groups"]
        fmt = lambda k: (f"{g[k]['n']}/{g[k]['exec']}/{g[k]['pnl_avg']}"
                         if g[k]["pnl_avg"] is not None else f"{g[k]['n']}/{g[k]['exec']}/-")
        L.append(f"| {name} | {fmt('lu')} | {fmt('nlu')} |")

    out = out_dir / "c11_time_report.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"报告: {out}")
    print(json.dumps({"checks": checks, "passed": passed,
                      "paired": {k: {w: (v[w] or {}).get("mean") for w in ("full", "c8")}
                                 for k, v in paired.items()}}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
