# -*- coding: utf-8 -*-
"""C16 第 4 步：决定性对照——7-10 月「T+1 开盘买、T+2 开盘卖」是不是只有涨停股才亏。

如果全市场普通股同口径也是同样负值，说明是市场层面的隔夜/日内定价问题，
换标的池没用；如果普通股接近 0，说明是涨停股特有的次日抛压，可以换池子。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
FEE = 0.15
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
WIN = ("2026-07-01", "2026-10-30")

q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
nxt = {td[i]: td[i + 1] for i in range(len(td) - 1)}

qi = q.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])
sub = q[(q['date'] >= WIN[0]) & (q['date'] <= WIN[1])].copy()
sub = sub[sub['thscode'].str[:3].isin(MAIN)]
sub['T1'] = sub['date'].map(nxt)
sub['T2'] = sub['T1'].map(nxt)
sub = sub[sub['T1'].notna() & sub['T2'].notna()].reset_index(drop=True)


def fetch(keys_code, keys_date, col):
    idx = pd.MultiIndex.from_arrays([keys_code, keys_date])
    return qi[col].reindex(idx).to_numpy()


sub['o1'] = fetch(sub['thscode'], sub['T1'], 'open')
sub['c1'] = fetch(sub['thscode'], sub['T1'], 'close')
sub['o2'] = fetch(sub['thscode'], sub['T2'], 'open')
# 当日涨幅（判断是否涨停）
sub['pct_t'] = (sub['close'] / fetch(sub['thscode'], sub['date'], 'close') * 0 - 1) if False else np.nan
sub = sub.dropna(subset=['o1', 'c1', 'o2'])
sub['open_ret'] = (sub['o1'] / sub['close'] - 1) * 100
sub = sub[sub['open_ret'].between(0, 5)]
sub['net'] = (sub['o2'] / sub['o1'] - 1) * 100 - FEE
sub['overnight'] = (sub['o2'] / sub['c1'] - 1) * 100
sub['intraday'] = (sub['c1'] / sub['o1'] - 1) * 100

# 标记 T 日涨停（用前收判断 10cm）
prev = fetch(sub['thscode'], sub['date'], 'open')  # 占位
sub['pct_day'] = (sub['close'] / fetch(sub['thscode'], sub['date'], 'close') - 1) * 100
zt = pd.read_parquet(BASE / "runs" / "c16_new_strategy" / "samples_0710.parquet")
zt_keys = set(zip(zt['thscode'], zt['T']))
sub['is_zt'] = [(c, d) in zt_keys for c, d in zip(sub['thscode'], sub['date'])]

print("=" * 104)
print("对照实验：7-10 月主板，T+1 开盘(0~5%)买 → T+2 开盘卖，含费 0.15%")
print("=" * 104)
print(f"{'样本':<22}{'n':>8}{'净均':>9}{'t':>7}{'日内':>9}{'隔夜':>9}{'胜率':>7}")
for label, m in [("全部（含涨停日）", np.ones(len(sub), bool)),
                 ("非涨停日股票", ~sub['is_zt'].to_numpy()),
                 ("涨停日股票", sub['is_zt'].to_numpy())]:
    g = sub[m]
    if len(g) < 100:
        continue
    t = g['net'].mean() / (g['net'].std(ddof=1) / np.sqrt(len(g)))
    print(f"{label:<22}{len(g):>8}{g['net'].mean():>+8.3f}%{t:>+7.1f}"
          f"{g['intraday'].mean():>+8.3f}%{g['overnight'].mean():>+8.3f}%{g['net'].gt(0).mean()*100:>6.1f}%")

print()
print("分日聚合（消除同日相关性后的 t 值，更保守）")
print("=" * 104)
for label, m in [("非涨停日股票", ~sub['is_zt'].to_numpy()),
                 ("涨停日股票", sub['is_zt'].to_numpy())]:
    g = sub[m]
    daily = g.groupby('date')['net'].mean()
    t = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))
    print(f"{label:<22} 日数={len(daily):<3} 日均值={daily.mean():+.3f}%  t(日级)={t:+.1f}")

sub[['date', 'thscode', 'open_ret', 'net', 'overnight', 'intraday', 'is_zt']].to_parquet(
    BASE / "runs" / "c16_new_strategy" / "market_baseline_0710.parquet", index=False)
print("\n写入 runs/c16_new_strategy/market_baseline_0710.parquet")
