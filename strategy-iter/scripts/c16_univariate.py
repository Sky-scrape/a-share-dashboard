# -*- coding: utf-8 -*-
"""C16 第 2 步：单变量扫描（训练段 7-8 月 / 验证段 9-10 月）。

纪律：
- 只用单变量分桶，不做多变量组合搜索（组合必然过拟合）；
- 每桶给出 n、均值、t 值；判据是「训练段为正 且 验证段同向」才登记；
- 基线 = 全体可成交样本的均值，所有提升都相对它算。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
p = pd.read_parquet(BASE / "runs" / "c16_new_strategy" / "samples_0710.parquet")
f = p[p['fillable']].copy()
f['seg'] = np.where(f['T'] < '2026-09-01', 'train 07-08', 'test 09-10')

TR = f[f['seg'] == 'train 07-08']
TE = f[f['seg'] == 'test 09-10']
print(f"可成交 n={len(f)}  train={len(TR)}  test={len(TE)}")
print(f"基线 T+2开盘卖：train {TR['net_open'].mean():+.2f}%  test {TE['net_open'].mean():+.2f}%  全 {f['net_open'].mean():+.2f}%")
print()


def show(name, conds):
    print(f"—— {name} " + "-" * (100 - len(name)))
    print(f"{'桶':<16}{'n训':>5}{'训均':>8}{'训t':>7}{'相对训基线':>11}{'n测':>5}{'测均':>8}{'相对测基线':>11}")
    base_tr = TR['net_open'].mean()
    base_te = TE['net_open'].mean()
    for label, c in conds:
        a, b = TR.loc[c, 'net_open'], TE.loc[c, 'net_open']
        if len(a) < 20:
            continue
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 else float('nan')
        print(f"{label:<16}{len(a):>5}{a.mean():>+7.2f}%{t:>+7.2f}{a.mean()-base_tr:>+9.2f}pp"
              f"{len(b):>5}{b.mean():>+7.2f}%{b.mean()-base_te:>+9.2f}pp")
    print()


show("连板数", [(f"cnt={i}", f['cnt'] == i) for i in (1, 2, 3)] +
     [("cnt>=4", f['cnt'] >= 4)])
show("涨停时间", [("早盘 <=10:00", f['early']), ("午盘 10:00-13:30", ~f['early'] & ~f['late']),
              ("尾盘 >=13:30", f['late'])])
show("封单比（seal/成交额）", [("0-5%", f['seal_ratio'] < 0.05), ("5-10%", (f['seal_ratio'] >= 0.05) & (f['seal_ratio'] < 0.10)),
                          ("10-20%", (f['seal_ratio'] >= 0.10) & (f['seal_ratio'] < 0.20)),
                          (">=20%", f['seal_ratio'] >= 0.20)])
show("炸板次数", [("未开板", ~f['reopen']), ("开板 1", f['open_times'] == 1),
              ("开板 >=2", f['open_times'] >= 2)])
show("成交额（亿）", [("<2", f['amt_yi'] < 2), ("2-5", (f['amt_yi'] >= 2) & (f['amt_yi'] < 5)),
                 ("5-10", (f['amt_yi'] >= 5) & (f['amt_yi'] < 10)), (">=10", f['amt_yi'] >= 10)])
show("次日开盘幅度", [("0-1%", f['open_ret'] < 1), ("1-2.5%", (f['open_ret'] >= 1) & (f['open_ret'] < 2.5)),
                 ("2.5-4%", (f['open_ret'] >= 2.5) & (f['open_ret'] < 4)), ("4-5%", f['open_ret'] >= 4)])
show("60 日涨幅位置", [("<0%", f['ret60'] < 0), ("0-20%", (f['ret60'] >= 0) & (f['ret60'] < 20)),
                  ("20-50%", (f['ret60'] >= 20) & (f['ret60'] < 50)), (">=50%", f['ret60'] >= 50)])
show("情绪扩张日", [("非扩张日", ~f['lu_expand'].fillna(False)), ("扩张日", f['lu_expand'].fillna(False))])
show("当日主板涨停家数", [("<40", f['lu_cnt_main'] < 40), ("40-70", (f['lu_cnt_main'] >= 40) & (f['lu_cnt_main'] < 70)),
                    (">=70", f['lu_cnt_main'] >= 70)])

# 两两交叉：连板 × 时间（只看训练段有量的组合）
print("—— 交叉：连板数 × 涨停时间 " + "-" * 70)
for c in (1, 2):
    for lab, cond in (("早盘", f['early']), ("非早盘", ~f['early'])):
        a, b = TR[(TR['cnt'] == c) & cond], TE[(TE['cnt'] == c) & cond]
        if len(a) < 20:
            continue
        t = a['net_open'].mean() / (a['net_open'].std(ddof=1) / np.sqrt(len(a)))
        print(f"cnt={c} {lab:<8} n训={len(a):<4} 训均={a['net_open'].mean():+6.2f}% t={t:+.2f}  "
              f"n测={len(b):<4} 测均={b['net_open'].mean():+6.2f}%")
