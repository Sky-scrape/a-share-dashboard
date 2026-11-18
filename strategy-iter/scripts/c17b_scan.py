# -*- coding: utf-8 -*-
"""C17b 第 2 步：把真实换手率/流通市值/筹码交换度并入面板，重扫。

这次从一开始就在「可交易」子集上做（T 日未涨停 + T+1 非一字板），
不再重复 C17 的涨停污染。

新增特征（真数据，不再是近似）：
- turn        换手率（%）
- turn_ma5/20 换手率均线
- turn_spike  换手率突变 = turn / turn_ma20
- turn_cum60  60 日累计换手（筹码交换程度：>200% 说明流通盘换过两遍）
- turn_std60  换手率波动（活跃度稳定性）
- cap         流通市值（亿）
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"

d = pd.read_parquet(OUT / "panel_merged.parquet")
tc = pd.read_parquet(OUT / "turn_cap.parquet")
d = d.merge(tc, on=['thscode', 'date'], how='left')
print(f"面板 {len(d)}，换手率覆盖 {d['turn'].notna().mean()*100:.1f}%")

# 需要更长历史算换手率均线 → 从全量 qfq 重新算并回填
q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
q = q.sort_values(['thscode', 'date'])
sh = pd.read_parquet(OUT / "shares.parquet")
share = sh.drop_duplicates('code6').set_index('code6')['float_share'].to_dict()
q['fs'] = q['thscode'].str[:6].map(share)
q['turn'] = (q['volume'] / q['fs'] * 100).clip(upper=100)
g = q.groupby('thscode', sort=False)
q['turn_ma5'] = g['turn'].transform(lambda s: s.rolling(5).mean())
q['turn_ma20'] = g['turn'].transform(lambda s: s.rolling(20).mean())
q['turn_cum60'] = g['turn'].transform(lambda s: s.rolling(60).sum())
q['turn_std60'] = g['turn'].transform(lambda s: s.rolling(60).std())
q['turn_spike'] = q['turn'] / q['turn_ma20'].replace(0, np.nan)
q['turn_trend'] = q['turn_ma5'] / q['turn_ma20'].replace(0, np.nan)
feat = q[['thscode', 'date', 'turn', 'turn_ma5', 'turn_ma20', 'turn_cum60',
          'turn_std60', 'turn_spike', 'turn_trend']]

d = d.drop(columns=[c for c in ('turn', 'cap') if c in d.columns])
d = d.merge(feat, on=['thscode', 'date'], how='left').merge(tc, on=['thscode', 'date'], how='left')
d.to_parquet(OUT / "panel_full.parquet", index=False)

T = d[d['tradable']].copy()
TR, TE = T['seg'] == 'train', T['seg'] == 'test'
b_tr, b_te = T.loc[TR, 'r_oc'].mean(), T.loc[TE, 'r_oc'].mean()


def dt_t(sub):
    s = sub.groupby('date')['r_oc'].mean()
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 4 else np.nan


NEW = ['turn', 'turn_ma5', 'turn_ma20', 'turn_cum60', 'turn_std60', 'turn_spike',
       'turn_trend', 'cap']
print(f"\n可交易样本 {len(T)}；基线 train {b_tr:+.3f}%  test {b_te:+.3f}%")
print()
print("=" * 104)
print("真实筹码/换手率特征（5 分位分桶，可交易子集）")
print("=" * 104)
print(f"{'特征':<12}{'桶':<5}{'n训':>6}{'Δ训':>8}{'t训':>7}{'n测':>6}{'Δ测':>8}{'t测':>7}{'判读':>6}")
rows = []
for c in NEW:
    if c not in T.columns or T[c].notna().sum() < 10000:
        continue
    try:
        qb = pd.qcut(T[c], 5, labels=False, duplicates='drop')
    except Exception:
        continue
    for i in range(int(qb.max()) + 1):
        m = (qb == i).fillna(False)
        a, b = T.loc[m & TR, 'r_oc'], T.loc[m & TE, 'r_oc']
        if len(a) < 300 or len(b) < 150:
            continue
        da, db_ = a.mean() - b_tr, b.mean() - b_te
        ta, tb = dt_t(T[m & TR]), dt_t(T[m & TE])
        ok = da > 0.3 and db_ > 0 and ta > 2 and tb > 1
        rows.append(dict(feat=c, bucket=f"Q{i+1}", n_tr=len(a), d_tr=da, t_tr=ta,
                         n_te=len(b), d_te=db_, t_te=tb, ok=ok))
        print(f"{c:<12}Q{i+1:<4}{len(a):>6}{da:>+7.2f}pp{ta:>+7.2f}{len(b):>6}"
              f"{db_:>+7.2f}pp{tb:>+7.2f}{'✓' if ok else '':>6}")

r = pd.DataFrame(rows)
r.sort_values('d_tr', ascending=False).to_csv(OUT / "scan_turn.csv", index=False,
                                              encoding="utf-8-sig")
print()
print(f"四判据全过：{int(r['ok'].sum())} / {len(r)}")
