# -*- coding: utf-8 -*-
"""C18 第 1 步：小市值池内的全特征重扫。

前提：用户优先买小市值。小市值（游资主导、题材驱动）的资金结构和定价方式与大盘股
显著不同，同样的因子在大池子里被稀释、在小池子里可能才露出方向。

池子定义（可调，先给三档看规模）：
- 微盘：流通市值 < 20 亿
- 小盘：< 30 亿
- 中小：< 50 亿

全部在「可交易」子集上做：T 日未涨停（chg<9.5）且 T+1 非一字板（隔夜<9.5）。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c18_small"
OUT.mkdir(parents=True, exist_ok=True)
d = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_full.parquet")
T = d[d['tradable']].copy()

print("=" * 100)
print("小市值池规模（可交易样本）")
print("=" * 100)
print(f"{'池子':<18}{'n':>8}{'占比':>8}{'train均':>10}{'test均':>10}{'日均只数':>10}")
for lab, thr in [("微盘 <20亿", 20), ("小盘 <30亿", 30), ("中小 <50亿", 50), ("全市场", 99999)]:
    g = T[T['cap'] < thr]
    tr, te = g[g.seg == 'train'], g[g.seg == 'test']
    print(f"{lab:<18}{len(g):>8}{len(g)/len(T)*100:>7.1f}%{tr['r_oc'].mean():>+9.3f}%"
          f"{te['r_oc'].mean():>+9.3f}%{g.groupby('date').size().mean():>10.0f}")

POOL = T[T['cap'] < 30].copy()
POOL.to_parquet(OUT / "pool_small.parquet", index=False)
TR, TE = POOL['seg'] == 'train', POOL['seg'] == 'test'
b_tr, b_te = POOL.loc[TR, 'r_oc'].mean(), POOL.loc[TE, 'r_oc'].mean()
print(f"\n选定池：<30 亿，n={len(POOL)}，基线 train {b_tr:+.3f}%  test {b_te:+.3f}%")


def dt_t(sub):
    s = sub.groupby('date')['r_oc'].mean()
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 4 else np.nan


FEAT = ['body', 'upper', 'lower', 'gap', 'chg', 'amp', 'ma_bull', 'dev20', 'dev60',
        'ret20', 'ret60', 'pos60', 'to_hi60', 'new_hi60', 'volr', 'vol_trend',
        'amt_rank60', 'amt_yi', 'vol_price_div', 'vol_up', 'shrink_pull',
        'profit20', 'profit60', 'above_ratio', 'cost_disp', 'atr_pct', 'rel_str',
        'hot_rank', 'hot_on', 'hot_new', 'dt_on', 'dt_org_net', 'dt_net_rate',
        'dt_org_pos', 'dt_net_pos', 'dt_net_ratio',
        'turn', 'turn_ma5', 'turn_ma20', 'turn_cum60', 'turn_std60', 'turn_spike',
        'turn_trend']
BOOL = {'ma_bull', 'new_hi60', 'vol_price_div', 'vol_up', 'shrink_pull',
        'hot_on', 'hot_new', 'dt_on', 'dt_org_pos', 'dt_net_pos'}

rows = []
for c in FEAT:
    if c not in POOL.columns or POOL[c].notna().sum() < 5000:
        continue
    x = POOL[c]
    if c in BOOL:
        groups = [("=1", x == 1), ("=0", x == 0)]
    else:
        try:
            qb = pd.qcut(x, 5, labels=False, duplicates='drop')
        except Exception:
            continue
        if not qb.notna().any():
            continue
        groups = [(f"Q{i+1}", qb == i) for i in range(int(qb.max()) + 1)]
    for lab, m in groups:
        m = m.fillna(False)
        a, b = POOL.loc[m & TR, 'r_oc'], POOL.loc[m & TE, 'r_oc']
        if len(a) < 200 or len(b) < 100:
            continue
        da, db_ = a.mean() - b_tr, b.mean() - b_te
        rows.append(dict(feat=c, bucket=lab, n_tr=len(a), m_tr=a.mean(), d_tr=da,
                         t_tr=dt_t(POOL[m & TR]), n_te=len(b), m_te=b.mean(), d_te=db_,
                         t_te=dt_t(POOL[m & TE])))
r = pd.DataFrame(rows)
r['ok'] = (r['d_tr'] > 0.4) & (r['d_te'] > 0) & (r['t_tr'] > 2) & (r['t_te'] > 1)
r.sort_values('d_tr', ascending=False).to_csv(OUT / "scan_small.csv", index=False,
                                              encoding="utf-8-sig")

print()
print("=" * 108)
print(f"小市值池特征扫描（{len(r)} 桶，四判据：Δ训>0.4pp / Δ测>0 / t训>2 / t测>1）")
print("=" * 108)
print(f"{'特征':<14}{'桶':<5}{'n训':>6}{'Δ训':>8}{'t训':>7}{'n测':>5}{'Δ测':>8}{'t测':>7}{'':>4}")
for x in r.nlargest(18, 'd_tr').itertuples():
    print(f"{x.feat:<14}{x.bucket:<5}{x.n_tr:>6}{x.d_tr:>+7.2f}pp{x.t_tr:>+7.2f}"
          f"{x.n_te:>5}{x.d_te:>+7.2f}pp{x.t_te:>+7.2f}{'✓' if x.ok else '':>4}")
print()
print(f"四判据全过：{int(r['ok'].sum())} / {len(r)}")
for x in r[r['ok']].itertuples():
    print(f"  ✓ {x.feat} {x.bucket}  Δ训={x.d_tr:+.2f}pp(t={x.t_tr:+.1f})  "
          f"Δ测={x.d_te:+.2f}pp(t={x.t_te:+.1f})  n={x.n_tr}/{x.n_te}")
