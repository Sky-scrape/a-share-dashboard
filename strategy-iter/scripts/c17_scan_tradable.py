# -*- coding: utf-8 -*-
"""C17 第 7 步：在「可交易」子集上重跑全部 40 个特征。

前三步的扫描全部被涨停股污染：new_hi60 的 1354 笔里 1157 笔是 T 日涨停（次日一字板），
剔除后只剩 197 笔且均值为 −0.85%。所以任何特征都必须在 tradable 子集上重测。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"
d = pd.read_parquet(OUT / "panel_merged.parquet")
T = d[d['tradable']].copy()
TR, TE = T['seg'] == 'train', T['seg'] == 'test'
b_tr, b_te = T.loc[TR, 'r_oc'].mean(), T.loc[TE, 'r_oc'].mean()


def dt_t(sub):
    s = sub.groupby('date')['r_oc'].mean()
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 4 else np.nan


FEAT = ['body', 'upper', 'lower', 'gap', 'chg', 'amp', 'ma_bull', 'dev20', 'dev60',
        'ret20', 'ret60', 'pos60', 'to_hi60', 'new_hi60', 'volr', 'vol_trend',
        'amt_rank60', 'amt_yi', 'vol_price_div', 'vol_up', 'shrink_pull',
        'profit20', 'profit60', 'above_ratio', 'cost_disp', 'atr_pct', 'rel_str',
        'hot_rank', 'hot_on', 'hot_new', 'dt_on', 'dt_org_net', 'dt_net_rate',
        'dt_org_pos', 'dt_net_pos', 'dt_net_ratio']
BOOL = {'ma_bull', 'new_hi60', 'vol_price_div', 'vol_up', 'shrink_pull',
        'hot_on', 'hot_new', 'dt_on', 'dt_org_pos', 'dt_net_pos'}

rows = []
for c in FEAT:
    if c not in T.columns:
        continue
    x = T[c]
    if c in BOOL:
        groups = [("=1", x == 1), ("=0", x == 0)]
    else:
        try:
            q = pd.qcut(x, 5, labels=False, duplicates='drop')
        except Exception:
            continue
        if not q.notna().any():
            continue
        groups = [(f"Q{i+1}", q == i) for i in range(int(q.max()) + 1)]
    for lab, m in groups:
        m = m.fillna(False)
        a, b = T.loc[m & TR, 'r_oc'], T.loc[m & TE, 'r_oc']
        if len(a) < 300 or len(b) < 150:
            continue
        rows.append(dict(feat=c, bucket=lab, n_tr=len(a), m_tr=a.mean(), t_tr=dt_t(T[m & TR]),
                         n_te=len(b), m_te=b.mean(), t_te=dt_t(T[m & TE]),
                         d_tr=a.mean() - b_tr, d_te=b.mean() - b_te))
r = pd.DataFrame(rows)
r['ok'] = (r['d_tr'] > 0.3) & (r['d_te'] > 0) & (r['t_tr'] > 2) & (r['t_te'] > 1)
r.sort_values('d_tr', ascending=False).to_csv(OUT / "scan_tradable.csv", index=False,
                                              encoding="utf-8-sig")

print(f"可交易样本 {len(T)}；基线 train {b_tr:+.3f}%  test {b_te:+.3f}%")
print(f"扫描 {len(r)} 个桶，四判据全过（Δ训>0.3pp, Δ测>0, t训>2, t测>1）：{int(r['ok'].sum())}")
print()
print("=" * 106)
print("训练段提升最大的 15 个桶（可交易子集）")
print("=" * 106)
print(f"{'特征':<15}{'桶':<5}{'n训':>6}{'Δ训':>8}{'t训':>7}{'n测':>6}{'Δ测':>8}{'t测':>7}{'通过':>5}")
for x in r.nlargest(15, 'd_tr').itertuples():
    print(f"{x.feat:<15}{x.bucket:<5}{x.n_tr:>6}{x.d_tr:>+7.2f}pp{x.t_tr:>+7.2f}"
          f"{x.n_te:>6}{x.d_te:>+7.2f}pp{x.t_te:>+7.2f}{'✓' if x.ok else '':>5}")
print()
print("=" * 106)
print("四判据全过的桶")
print("=" * 106)
ok = r[r['ok']].sort_values('d_te', ascending=False)
if not len(ok):
    print("  （无——在可交易子集里，40 个特征 140 个桶没有一个能同时通过训练段和验证段）")
for x in ok.itertuples():
    print(f"  {x.feat} {x.bucket}  Δ训={x.d_tr:+.2f}pp(t={x.t_tr:+.1f})  "
          f"Δ测={x.d_te:+.2f}pp(t={x.t_te:+.1f})  n={x.n_tr}/{x.n_te}")
print()
print("对照组：训练段最强的桶在验证段还剩多少（过拟合衰减）")
top = r.nlargest(10, 'd_tr')
print(f"  top10 训练段平均 Δ = {top['d_tr'].mean():+.2f}pp")
print(f"  top10 验证段平均 Δ = {top['d_te'].mean():+.2f}pp")
print(f"  衰减 = {top['d_te'].mean() - top['d_tr'].mean():+.2f}pp")
