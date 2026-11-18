# -*- coding: utf-8 -*-
"""C18 第 3 步：每日横截面选 Top N（标准多因子做法，区别于绝对阈值分桶）。

前面的扫描都是「全样本按绝对阈值分桶」——比如「换手率 < 20% 分位」。这和实盘选股不一样：
实盘是**每天**在候选池里挑最好的几只。横截面排序会自动适应市场整体水位，
在极端月份的表现可能完全不同。

做法：每个交易日，在小市值可交易池里按某因子排序，取 Top N 等权买入
（T 日收盘买 → T+1 收盘卖），算每日组合收益，再检验。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c18_small"
P = pd.read_parquet(OUT / "pool_small_exit.parquet")

FACTORS = {
    "turn_cum60 低": ('turn_cum60', True),
    "turn 低": ('turn', True),
    "turn_std60 低": ('turn_std60', True),
    "amt_yi 低": ('amt_yi', True),
    "amt_yi 高": ('amt_yi', False),
    "volr 低": ('volr', True),
    "volr 高": ('volr', False),
    "ret20 低": ('ret20', True),
    "ret20 高": ('ret20', False),
    "rel_str 高": ('rel_str', False),
    "profit20 高": ('profit20', False),
    "above_ratio 低": ('above_ratio', True),
    "new_hi60": ('new_hi60', False),
    "turn_spike 低": ('turn_spike', True),
}


def topn(col, asc, n):
    """每日按 col 排序取 TopN，返回每日组合收益序列。"""
    x = P[['date', 'seg', 'r_oc', col]].dropna(subset=[col]).copy()
    x['rk'] = x.groupby('date')[col].rank(ascending=asc, method='first')
    sel = x[x['rk'] <= n]
    return sel.groupby(['date', 'seg'])['r_oc'].mean().reset_index()


def report(sel, label):
    tr = sel[sel.seg == 'train']['r_oc']
    te = sel[sel.seg == 'test']['r_oc']
    if len(tr) < 20 or len(te) < 20:
        return None
    t_tr = tr.mean() / (tr.std(ddof=1) / np.sqrt(len(tr)))
    t_te = te.mean() / (te.std(ddof=1) / np.sqrt(len(te)))
    return dict(label=label, d_tr=len(tr), m_tr=tr.mean(), t_tr=t_tr,
                d_te=len(te), m_te=te.mean(), t_te=t_te,
                m_all=sel['r_oc'].mean(), win=sel['r_oc'].gt(0).mean() * 100)


print("=" * 108)
print("每日横截面选 Top N（小市值 <30亿，T 日收盘买 → T+1 收盘卖）")
print(f"池子日均可选 {P.groupby('date').size().mean():.0f} 只")
print("=" * 108)
rows = []
for n in (3, 5, 10):
    print(f"\n—— Top {n} " + "-" * 96)
    print(f"{'因子':<18}{'训日数':>7}{'训日均':>9}{'t':>7}{'测日数':>7}{'测日均':>9}{'t':>7}{'全均':>8}{'胜率':>7}")
    for lab, (col, asc) in FACTORS.items():
        sel = topn(col, asc, n)
        r = report(sel, f"{lab} top{n}")
        if r:
            rows.append(dict(factor=lab, n=n, **{k: v for k, v in r.items() if k != 'label'}))
            print(f"{lab:<18}{r['d_tr']:>7}{r['m_tr']:>+8.2f}%{r['t_tr']:>+7.2f}"
                  f"{r['d_te']:>7}{r['m_te']:>+8.2f}%{r['t_te']:>+7.2f}"
                  f"{r['m_all']:>+7.2f}%{r['win']:>6.1f}%")

r = pd.DataFrame(rows)
r['ok'] = (r['m_tr'] > 0.15) & (r['m_te'] > 0) & (r['t_tr'] > 1.5) & (r['t_te'] > 0)
r.sort_values('m_tr', ascending=False).to_csv(OUT / "topn.csv", index=False,
                                              encoding="utf-8-sig")
print()
print("=" * 108)
print("同时满足「训练段 >+0.15% 且 验证段 >0」的组合")
print("=" * 108)
ok = r[r['ok']].sort_values('m_te', ascending=False)
if not len(ok):
    print("  （无）")
for x in ok.itertuples():
    print(f"  {x.factor} top{x.n}  训 {x.m_tr:+.2f}%(t={x.t_tr:+.1f})  "
          f"测 {x.m_te:+.2f}%(t={x.t_te:+.1f})  全 {x.m_all:+.2f}%")

print()
print("基线对照：小市值池整体 train +0.129% / test -0.474%")
print(f"训练段最优 5 个组合在验证段的表现：")
top5 = r.nlargest(5, 'm_tr')
for x in top5.itertuples():
    print(f"  {x.factor:<18} top{x.n:<3} 训 {x.m_tr:+.2f}% → 测 {x.m_te:+.2f}%  （衰减 {x.m_te-x.m_tr:+.2f}pp）")
