# -*- coding: utf-8 -*-
"""C18 第 2 步：离场规则网格（止盈/止损），在小市值池上。

前几轮全部在「选股」上加特征，这轮改**执行**：T 日收盘买入后，T+1 日内怎么卖。

可执行性说明：
- 止盈：挂限价单在 +X%，T+1 最高价触及即成交（用 high 判定，成交价取 +X%，不取 high）
- 止损：挂条件单在 -Y%，T+1 最低价跌破即成交（用 low 判定，成交价取 -Y%）
- 都没触发 → T+1 收盘卖
- 若同日既触止盈又触止损（无法从日线判断先后），按**止损优先**处理（保守）

池子：主板可交易样本，流通市值 < 30 亿。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c18_small"
P = pd.read_parquet(OUT / "pool_small.parquet")
TR, TE = P['seg'] == 'train', P['seg'] == 'test'

# T+1 相对买入价（T 日收盘）的高低点
P['h1_rel'] = (P['high'] * 0 + P['h1']) if 'h1' in P.columns else np.nan
# 面板里没有 T+1 的 high/low，重新取
q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
qi = q.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])
P['h1'] = qi['high'].reindex(pd.MultiIndex.from_arrays([P['thscode'], P['T1']])).to_numpy()
P['l1'] = qi['low'].reindex(pd.MultiIndex.from_arrays([P['thscode'], P['T1']])).to_numpy()
P['h1_rel'] = (P['h1'] / P['close'] - 1) * 100
P['l1_rel'] = (P['l1'] / P['close'] - 1) * 100

FEE = 0.15


def exit_ret(tp, sl):
    """止盈 tp% / 止损 sl%（相对买入价），返回净收益序列。"""
    hit_tp = P['h1_rel'] >= tp
    hit_sl = P['l1_rel'] <= sl
    r = P['r_oc'] + FEE                       # 还原成不含费
    r = np.where(hit_tp, tp, r)
    r = np.where(hit_sl, sl, r)               # 止损优先（同日双触发时保守）
    return pd.Series(r - FEE, index=P.index)


def show(col, label):
    a, b = P.loc[TR & P[col].notna(), col], P.loc[TE & P[col].notna(), col]
    if len(a) < 100:
        return
    def tt(g):
        s = g.groupby(P.loc[g.index, 'date']).mean()
        return s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))
    print(f"{label:<22}{len(a):>6}{a.mean():>+8.2f}%{tt(a):>+7.2f}{len(b):>6}"
          f"{b.mean():>+8.2f}%{tt(b):>+7.2f}{P[col].mean():>+8.2f}%{P[col].gt(0).mean()*100:>7.1f}%")


print("=" * 104)
print("小市值池（<30亿）离场规则网格：T 日收盘买 → T+1 按规则卖")
print("=" * 104)
print(f"{'规则':<22}{'n训':>6}{'训均':>9}{'t':>7}{'n测':>6}{'测均':>9}{'t':>7}{'全均':>9}{'胜率':>8}")
P['base'] = P['r_oc']
show('base', '基准：T+1收盘卖')
print()
for tp in (2, 3, 5):
    for sl in (-2, -3, -5):
        P[f'e_{tp}_{sl}'] = exit_ret(tp, sl)
        show(f'e_{tp}_{sl}', f'止盈+{tp}% 止损{sl}%')
    print()

print("=" * 104)
print("只止损不止盈（让盈利单跑）")
print("=" * 104)
print(f"{'规则':<22}{'n训':>6}{'训均':>9}{'t':>7}{'n测':>6}{'测均':>9}{'t':>7}{'全均':>9}{'胜率':>8}")
for sl in (-2, -3, -4, -5):
    P[f's_{sl}'] = exit_ret(999, sl)
    show(f's_{sl}', f'仅止损 {sl}%')
print()
print("=" * 104)
print("只止盈不止损")
print("=" * 104)
for tp in (2, 3, 5, 8):
    P[f't_{tp}'] = exit_ret(tp, -999)
    show(f't_{tp}', f'仅止盈 +{tp}%')

P.to_parquet(OUT / "pool_small_exit.parquet", index=False)
