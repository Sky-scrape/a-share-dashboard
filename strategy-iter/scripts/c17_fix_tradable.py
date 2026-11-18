# -*- coding: utf-8 -*-
"""C17 第 6 步：修正「可交易性」污染后重算。

问题：new_hi60 + volr<1 的样本里 50.6% 在 T 日就涨停（封单导致成交量萎缩），
T+1 又是一字板开盘 —— 这种票 T 日收盘买不进、T+1 开盘卖不出，收益全部不可交易。
前面的 +4.02% 是假的。

修正定义（两条都是硬约束，不是可选过滤）：
- tradable_T  : T 日涨幅 < 9.5%（没封板，收盘还能挂单成交）
- tradable_T1 : T+1 开盘涨幅 < 9.5%（不是一字板，能卖出）
只有两者都满足的样本才算「可交易」，所有统计一律在可交易子集上重算。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"
d = pd.read_parquet(OUT / "panel_merged.parquet")
d['month'] = d['date'].str[:7]

d['tradable_T'] = d['chg'] < 9.5
d['tradable_T1'] = d['overnight'] < 9.5
d['tradable'] = d['tradable_T'] & d['tradable_T1']
d.to_parquet(OUT / "panel_merged.parquet", index=False)

T = d[d['tradable']].copy()
print(f"全样本 {len(d)} → 可交易 {len(T)}（{len(T)/len(d)*100:.1f}%）")
print(f"被剔除：T 日涨停 {int((~d['tradable_T']).sum())}，T+1 一字板 {int((~d['tradable_T1']).sum())}")
print(f"可交易基线：全 {T['r_oc'].mean():+.3f}%  train {T[T.seg=='train']['r_oc'].mean():+.3f}%  "
      f"test {T[T.seg=='test']['r_oc'].mean():+.3f}%")

TR, TE = T['seg'] == 'train', T['seg'] == 'test'


def stat(g, label, width=28):
    if len(g) < 15:
        print(f"{label:<{width}}{len(g):>6}   样本不足")
        return
    s = g.groupby('date')['r_oc'].mean()
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan
    print(f"{label:<{width}}{len(g):>6}{g['r_oc'].mean():>+8.2f}%{g['r_oc'].gt(0).mean()*100:>7.1f}%"
          f"{t:>+8.2f}{len(s):>6}{g.groupby('date').size().mean():>7.1f}")


hdr = f"{'策略':<28}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}{'日均只数':>8}"
SIG = {
    "A 创新高":              T['new_hi60'] == 1,
    "B 新高+缩量":           (T['new_hi60'] == 1) & (T['volr'] < 1),
    "C 新高+小额<5亿":        (T['new_hi60'] == 1) & (T['amt_yi'] < 5),
    "D 新高+缩量+小额":       (T['new_hi60'] == 1) & (T['volr'] < 1) & (T['amt_yi'] < 5),
    "E 新高+缩量+小额+远均线":  (T['new_hi60'] == 1) & (T['volr'] < 1) & (T['amt_yi'] < 5) & (T['dev20'] > 10),
}
print()
print("=" * 110)
print("【修正后】只统计可交易样本")
print("=" * 110)
for name, m in SIG.items():
    m = m.fillna(False)
    print(f"\n【{name}】")
    print(hdr)
    stat(T[m], "  训练段 7-8")
    stat(T[m], "  验证段 9-10")
    stat(T[m], "  全窗口")
    break  # 只打印第一个做示例，下面统一表格

print()
print("=" * 110)
print("汇总（同一张表）")
print("=" * 110)
print(f"{'策略':<24}{'n训':>5}{'训均':>8}{'t':>6}{'n测':>5}{'测均':>8}{'t':>6}{'全n':>5}{'全均':>8}{'全t':>6}{'日均':>6}")
for name, m in SIG.items():
    m = m.fillna(False)
    a, b, c = T[m & TR], T[m & TE], T[m]
    if len(a) < 5 or len(b) < 5:
        print(f"{name:<24}  样本不足")
        continue

    def tt(g):
        s = g.groupby('date')['r_oc'].mean()
        return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan
    print(f"{name:<24}{len(a):>5}{a['r_oc'].mean():>+7.2f}%{tt(a):>+6.1f}{len(b):>5}"
          f"{b['r_oc'].mean():>+7.2f}%{tt(b):>+6.1f}{len(c):>5}{c['r_oc'].mean():>+7.2f}%"
          f"{tt(c):>+6.1f}{c.groupby('date').size().mean():>6.1f}")

print()
print("=" * 110)
print("逐月（策略 B：新高+缩量，修正后可交易）")
print("=" * 110)
print(hdr)
mb = ((T['new_hi60'] == 1) & (T['volr'] < 1)).fillna(False)
for mo, g in T[mb].groupby('month'):
    stat(g, f"  {mo}")
print(hdr)
for mo, g in T.groupby('month'):
    stat(g, f"  （可交易全市场 {mo}）")
