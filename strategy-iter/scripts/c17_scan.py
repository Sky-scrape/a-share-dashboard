# -*- coding: utf-8 -*-
"""C17 第 3 步：合并人气榜 + 龙虎榜 + 形态面板，然后全特征扫描。

时点纪律（防前视）：
- 交易模式 = T 日收盘买入 → T+1 收盘卖出；
- 人气榜/龙虎榜都是**盘后**才完整的数据，T 日收盘时刻拿不到当日版本，
  因此一律取 **T-1 日**的值（`_lag1` 后缀），宁可用旧数据也不引入未来信息。
"""
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"
d = pd.read_parquet(OUT / "panel_0710.parquet")
td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
prev = {td[i]: td[i - 1] for i in range(1, len(td))}

# ---- 热股榜 ----
hot = {}
for f in sorted(glob.glob(str(BASE / "raw" / "hot" / "hot_*.json"))):
    x = json.loads(Path(f).read_text(encoding="utf-8"))
    hot[x['date']] = x['ranks']
d['hot_key'] = d['date'].map(prev)
d['hot_rank'] = [hot.get(k, {}).get(c, np.nan) for k, c in zip(d['hot_key'], d['thscode'])]
d['hot_on'] = d['hot_rank'].notna().astype(float)
# 前一日是否上榜（判断"新进榜"）
prev2 = {td[i]: td[i - 2] for i in range(2, len(td))}
d['hot_rank_lag2'] = [hot.get(prev2.get(k), {}).get(c, np.nan) for k, c in zip(d['hot_key'], d['thscode'])]
d['hot_new'] = ((d['hot_on'] == 1) & d['hot_rank_lag2'].isna()).astype(float)

# ---- 龙虎榜 ----
dt_rows = []
for f in sorted(glob.glob(str(BASE / "raw" / "dt" / "dt_*.json"))):
    x = json.loads(Path(f).read_text(encoding="utf-8"))
    for it in x.get('stock_items') or []:
        dt_rows.append(dict(
            date=x.get('trade_date'), thscode=it.get('thscode'),
            dt_net=it.get('net_value'), dt_net_rate=it.get('net_rate'),
            dt_org_net=it.get('org_net_value'),
            dt_buy=it.get('buy_value'), dt_sell=it.get('sell_value'),
            dt_hot=it.get('hot_rank'), dt_days=it.get('range_days')))
dt = pd.DataFrame(dt_rows).drop_duplicates(['date', 'thscode'])
print("龙虎榜行", len(dt), "日期", dt['date'].min(), dt['date'].max())
d = d.merge(dt.rename(columns={'date': 'hot_key'}), on=['hot_key', 'thscode'], how='left')
d['dt_on'] = d['dt_net'].notna().astype(float)
d['dt_org_pos'] = (d['dt_org_net'] > 0).astype(float)
d['dt_net_pos'] = (d['dt_net'] > 0).astype(float)
# 净买占成交额
d['dt_net_ratio'] = d['dt_net'] / d['amount'].replace(0, np.nan)

d.to_parquet(OUT / "panel_merged.parquet", index=False)

# ==================== 扫描 ====================
TR, TE = d['seg'] == 'train', d['seg'] == 'test'
b_tr, b_te = d.loc[TR, 'r_oc'].mean(), d.loc[TE, 'r_oc'].mean()


def dt_t(sub):
    """日级 t：先按日取均值再检验，消除同日相关性。"""
    s = sub.groupby('date')['r_oc'].mean()
    if len(s) < 5:
        return np.nan
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))


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
    if c not in d.columns:
        continue
    x = d[c]
    if c in BOOL:
        groups = [("=1", x == 1), ("=0", x == 0)]
    else:
        try:
            q = pd.qcut(x, 5, labels=False, duplicates='drop')
        except Exception:
            continue
        groups = [(f"Q{i+1}", q == i) for i in range(int(q.max()) + 1 if q.notna().any() else 0)]
    for lab, m in groups:
        m = m.fillna(False)
        a, b = d.loc[m & TR, 'r_oc'], d.loc[m & TE, 'r_oc']
        if len(a) < 300 or len(b) < 150:
            continue
        rows.append(dict(feat=c, bucket=lab, n_tr=len(a), m_tr=a.mean(),
                         t_tr=dt_t(d[m & TR]), n_te=len(b), m_te=b.mean(),
                         t_te=dt_t(d[m & TE]), d_tr=a.mean() - b_tr,
                         d_te=b.mean() - b_te))
r = pd.DataFrame(rows)
r['ok'] = (r['d_tr'] > 0.3) & (r['d_te'] > 0) & (r['t_tr'] > 2)
r.to_csv(OUT / "scan.csv", index=False, encoding="utf-8-sig")

print(f"\n基线：train {b_tr:+.3f}%  test {b_te:+.3f}%")
print(f"扫描 {len(r)} 个桶，三条判据全过 {int(r['ok'].sum())} 个\n")
print("=" * 108)
print("训练段相对基线提升最大的 20 个桶")
print("=" * 108)
print(f"{'特征':<16}{'桶':<6}{'n训':>6}{'Δ训':>8}{'t训':>7}{'n测':>6}{'Δ测':>8}{'t测':>7}{'同向':>5}")
for x in r.nlargest(20, 'd_tr').itertuples():
    print(f"{x.feat:<16}{x.bucket:<6}{x.n_tr:>6}{x.d_tr:>+7.2f}pp{x.t_tr:>+7.2f}"
          f"{x.n_te:>6}{x.d_te:>+7.2f}pp{x.t_te:>+7.2f}{'✓' if x.d_te > 0 else '✗':>5}")

print()
print("=" * 108)
print("三条判据全过的桶（Δ训>0.3pp 且 Δ测>0 且 t训>2）")
print("=" * 108)
ok = r[r['ok']].sort_values('d_te', ascending=False)
if not len(ok):
    print("  （无）")
for x in ok.itertuples():
    print(f"{x.feat:<16}{x.bucket:<6}n训={x.n_tr:<6}Δ训={x.d_tr:+.2f}pp(t={x.t_tr:+.1f})  "
          f"n测={x.n_te:<6}Δ测={x.d_te:+.2f}pp(t={x.t_te:+.1f})")
