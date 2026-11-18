# -*- coding: utf-8 -*-
"""C16 第 3 步：组合搜索（训练段 7-8 月选，验证段 9-10 月验）。

这是个「过拟合演示」：在训练段上穷举 1~3 条件组合取最优，再看它在验证段还剩多少。
结论无论正反都有价值——如果最优组合在验证段仍然为正，那就是值得继续的线索；
如果崩掉，就证明 7-10 月窗口不支持参数搜索。
"""
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
p = pd.read_parquet(BASE / "runs" / "c16_new_strategy" / "samples_0710.parquet")
f = p[p['fillable']].copy()
f['seg'] = np.where(f['T'] < '2026-09-01', 'train', 'test')

CONDS = {
    "封单薄<10%":  f['seal_ratio'] < 0.10,
    "封单厚>=10%": f['seal_ratio'] >= 0.10,
    "位置低<20%":  f['ret60'] < 20,
    "位置高>=20%": f['ret60'] >= 20,
    "额2-10亿":    (f['amt_yi'] >= 2) & (f['amt_yi'] <= 10),
    "首板":        f['cnt'] == 1,
    "非首板":      f['cnt'] >= 2,
    "早盘封":      f['early'],
    "非扩张日":    ~f['lu_expand'].fillna(False),
    "涨停>=70家":  f['lu_cnt_main'] >= 70,
    "开盘<2.5%":   f['open_ret'] < 2.5,
}
TR = f['seg'] == 'train'
TE = f['seg'] == 'test'
MIN_N = 120
res = []
for k in (1, 2, 3):
    for combo in itertools.combinations(CONDS, k):
        m = np.ones(len(f), dtype=bool)
        for c in combo:
            m &= CONDS[c].fillna(False).to_numpy()
        a = f.loc[m & TR, 'net_open']
        if len(a) < MIN_N:
            continue
        b = f.loc[m & TE, 'net_open']
        res.append(dict(combo=" + ".join(combo), n_tr=len(a), m_tr=a.mean(),
                        t_tr=a.mean() / (a.std(ddof=1) / np.sqrt(len(a))),
                        n_te=len(b), m_te=b.mean() if len(b) else np.nan))
r = pd.DataFrame(res).sort_values('m_tr', ascending=False)
print(f"候选组合 {len(r)} 个（训练段 n>={MIN_N}）")
print("训练段基线 %.2f%%，验证段基线 %.2f%%" % (
    f.loc[TR, 'net_open'].mean(), f.loc[TE, 'net_open'].mean()))
print()
print("=" * 112)
print("训练段最优 12 个组合，及其在验证段的表现")
print("=" * 112)
print(f"{'组合':<46}{'n训':>5}{'训均':>8}{'训t':>7}{'n测':>5}{'测均':>8}{'测t':>7}{'衰减':>8}")
top = r.head(12)
for x in top.itertuples():
    te = f.loc[(f['seg'] == 'test') & np.all(
        [CONDS[c].fillna(False).to_numpy() for c in x.combo.split(" + ")], axis=0), 'net_open']
    tt = te.mean() / (te.std(ddof=1) / np.sqrt(len(te))) if len(te) > 2 else np.nan
    print(f"{x.combo:<46}{x.n_tr:>5}{x.m_tr:>+7.2f}%{x.t_tr:>+7.2f}{x.n_te:>5}"
          f"{x.m_te:>+7.2f}%{tt:>+7.2f}{x.m_te-x.m_tr:>+7.2f}pp")

print()
print("=" * 112)
print("训练段 vs 验证段：组合均值的相关性（过拟合的量化）")
print("=" * 112)
ok = r.dropna(subset=['m_te'])
print(f"可配对组合 {len(ok)} 个，相关系数 r = {ok['m_tr'].corr(ok['m_te']):.3f}")
print(f"训练段最优组合的验证段均值：{ok.nlargest(10,'m_tr')['m_te'].mean():+.2f}%")
print(f"全部组合的验证段均值：{ok['m_te'].mean():+.2f}%（基线 {f.loc[TE,'net_open'].mean():+.2f}%）")
print(f"训练段为正的组合数 {(ok['m_tr']>0).sum()} / {len(ok)}；"
      f"其中验证段仍为正的 {((ok['m_tr']>0)&(ok['m_te']>0)).sum()} 个")

print()
print("=" * 112)
print("如果只允许一个条件（最不容易过拟合）：训练段 vs 验证段")
print("=" * 112)
print(f"{'条件':<14}{'n训':>5}{'训均':>8}{'n测':>5}{'测均':>8}{'差':>9}")
for c in CONDS:
    a = f.loc[CONDS[c].fillna(False).to_numpy() & TR, 'net_open']
    b = f.loc[CONDS[c].fillna(False).to_numpy() & TE, 'net_open']
    if len(a) < 30 or len(b) < 30:
        continue
    print(f"{c:<14}{len(a):>5}{a.mean():>+7.2f}%{len(b):>5}{b.mean():>+7.2f}%{b.mean()-a.mean():>+8.2f}pp")

r.to_csv(BASE / "runs" / "c16_new_strategy" / "combos.csv", index=False, encoding="utf-8-sig")
