# -*- coding: utf-8 -*-
"""C16 第 5 步：T 日收盘买 → T+1 卖（全样本，无任何 T+1 条件）。

**前视偏差警告**：上一版曾在样本上加了「T+1 开盘涨 0~5%」的筛选再回看 T 日买入收益，
那是拿次日才知道的信息去挑样本，算出 +0.335% 是假的。本脚本不加任何未来条件，
样本 = 7-10 月主板全部股票全部交易日。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
FEE = 0.15
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")

q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
nxt = {td[i]: td[i + 1] for i in range(len(td) - 1)}
qi = q.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])

s = q[(q['date'] >= "2026-07-01") & (q['date'] <= "2026-10-30")].copy()
s = s[s['thscode'].str[:3].isin(MAIN)].reset_index(drop=True)
s['T1'] = s['date'].map(nxt)
s = s[s['T1'].notna()].reset_index(drop=True)


def fetch(code, date, col):
    return qi[col].reindex(pd.MultiIndex.from_arrays([code, date])).to_numpy()


s['o1'] = fetch(s['thscode'], s['T1'], 'open')
s['c1'] = fetch(s['thscode'], s['T1'], 'close')
# T 日涨幅（判断当日是否涨停，10cm 主板）
s['pct'] = (s['close'] / fetch(s['thscode'], s['date'], 'close') * 0 + 1)  # 占位，下面用 prev
s['prev_close'] = s.groupby('thscode')['close'].shift(1)
s['pct_t'] = (s['close'] / s['prev_close'] - 1) * 100
s = s.dropna(subset=['o1', 'c1', 'prev_close'])
s['is_zt'] = s['pct_t'] >= 9.8
s['r_open'] = (s['o1'] / s['close'] - 1) * 100 - FEE
s['r_close'] = (s['c1'] / s['close'] - 1) * 100 - FEE

print("=" * 100)
print("T 日收盘买入 → T+1 卖出（无任何未来条件，全样本）")
print("=" * 100)
print(f"{'样本':<20}{'n':>8}{'T+1开盘卖':>11}{'日级t':>8}{'T+1收盘卖':>11}{'日级t':>8}")
for lab, m in [("主板全部", np.ones(len(s), bool)), ("非涨停日", ~s['is_zt'].to_numpy()),
               ("涨停日(买不到)", s['is_zt'].to_numpy())]:
    g = s[m]
    for col in ('r_open', 'r_close'):
        pass
    d1 = g.groupby('date')['r_open'].mean()
    d2 = g.groupby('date')['r_close'].mean()
    t1 = d1.mean() / (d1.std(ddof=1) / np.sqrt(len(d1)))
    t2 = d2.mean() / (d2.std(ddof=1) / np.sqrt(len(d2)))
    print(f"{lab:<20}{len(g):>8}{g['r_open'].mean():>+10.3f}%{t1:>+8.1f}"
          f"{g['r_close'].mean():>+10.3f}%{t2:>+8.1f}")

print()
print("涨停股当日收盘价 = 涨停价，实盘封板买不进；非涨停日才是可交易样本。")
print(f"非涨停日胜率：开盘卖 {s[~s['is_zt']]['r_open'].gt(0).mean()*100:.1f}%  "
      f"收盘卖 {s[~s['is_zt']]['r_close'].gt(0).mean()*100:.1f}%")
print()
print("分月（非涨停日，T+1 开盘卖）：")
g = s[~s['is_zt']].copy()
g['month'] = g['date'].str[:7]
for m, x in g.groupby('month'):
    d = x.groupby('date')['r_open'].mean()
    print(f"  {m}  n={len(x):<7} 均值={x['r_open'].mean():+.3f}%  日级t={d.mean()/(d.std(ddof=1)/np.sqrt(len(d))):+.1f}")
