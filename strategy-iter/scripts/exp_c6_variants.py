# -*- coding: utf-8 -*-
"""C6 轮后实验变体（2026-09-09）：一次构建 store，多个规则变体共享同一 precompute。

变体（全部基于 C6，仅改一处，隔离单因子贡献；实验不进 VERSIONS 版本线）：
  expA_aggressive10  aggressive 配额 (2,0)->(1,0)：进攻档 LU 最弱桶（61.8%/+2.33 vs normal 72.9%）
  expB_minscore70    涨停/非涨停组 min_score 55->70：C6 得分分桶 60~70 分 5 只全亏，>=70 占 98%
  expC_nlu_amt09     非涨停组量能闸门 amt_ratio>=0.9：缩量日 NLU -0.24%(n=47) vs 放量 +2.13%
用法: cd strategy-iter && python -m scripts.exp_c6_variants
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.data import build_store, SEL_START, SEL_END
from engine.precompute import precompute
from engine.run_round import run_round
from engine import rules as R


def main():
    store = build_store()
    base = R.VERSIONS["C6"]
    pre = precompute(store, base)   # 变体全部沿用 C6 的 regime 口径，一份 precompute 复用

    variants = {}
    a = copy.deepcopy(base)
    a["version"] = "expA"
    a["quotas"] = {**base["quotas"], "aggressive": (1, 0)}
    variants["expA_aggressive10"] = a

    b = copy.deepcopy(base)
    b["version"] = "expB"
    b["lu_group"] = {**base["lu_group"], "min_score": 70.0}
    b["nlu_group"] = {**base["nlu_group"], "min_score": 70.0}
    variants["expB_minscore70"] = b

    c = copy.deepcopy(base)
    c["version"] = "expC"
    c["nlu_group"] = {**base["nlu_group"], "market_amt_ratio_min": 0.9}
    variants["expC_nlu_amt09"] = c

    # expC 的闸门在 select 层消费：select_non_lu 读 pre['ms'] 的 amt_ratio
    # 为不改 engine/select.py 主逻辑，这里以包装方式实现——见 _gate_nlu_by_amt

    out_root = Path("runs")
    summary = {}
    for name, cfg in variants.items():
        # 注册临时版本（不入库 rules.py）
        R.VERSIONS[cfg["version"]] = cfg
        if name == "expC_nlu_amt09":
            _patch_nlu_amt_gate(pre)
        stats = run_round(cfg["version"], out_root / name, store=store, pre=pre)
        o, lu, nlu = stats["overall"], stats["lu"], stats["nlu"]
        summary[name] = dict(overall=(o["win_rate"], o["avg_close"]),
                             lu=(lu["win_rate"], lu["avg_close"], lu["n"]),
                             nlu=(nlu["win_rate"], nlu["avg_close"], nlu["n"]),
                             picks=stats["total_picks"])
        print(f"{name}: 总 {o['win_rate']}/{o['avg_close']}  LU {lu['win_rate']}/{lu['avg_close']}"
              f"(n={lu['n']})  NLU {nlu['win_rate']}/{nlu['avg_close']}(n={nlu['n']})  "
              f"选股 {stats['total_picks']}")
    Path("runs/exp_c6_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


_patched = False


def _patch_nlu_amt_gate(pre):
    """给 select_non_lu 注入市场量能闸门：T 日全市场成交额/20日均值 < 0.9 时不选 NLU。

    run_round 顶层 `from .select import select_non_lu` 已绑定到 run_round 命名空间，
    补丁必须落在 engine.run_round.select_non_lu 上（仅本实验进程内生效）。"""
    global _patched
    if _patched:
        return
    _patched = True
    import pandas as pd
    # engine/__init__ 里 `from .run_round import run_round` 把包属性遮蔽成函数，
    # 必须经 sys.modules 拿到模块对象再补丁（run_round 函数体解析的是本模块全局名）。
    import sys as _sys
    RR = _sys.modules["engine.run_round"]

    orig = RR.select_non_lu

    def gated(pre_, store_, si_day, ms_row, date, quota, cfg):
        thr = cfg.get("market_amt_ratio_min")
        if thr is not None:
            v = None
            try:
                v = ms_row["amt_ratio"]
            except (KeyError, TypeError, ValueError):
                pass
            if v is None or pd.isna(v) or v < thr:
                return []
        return orig(pre_, store_, si_day, ms_row, date, quota, cfg)

    RR.select_non_lu = gated


if __name__ == "__main__":
    main()
