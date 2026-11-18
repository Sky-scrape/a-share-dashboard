# -*- coding: utf-8 -*-
"""C17 第 4 步：new_hi60（收盘创 60 日新高）的稳健性检验。

为什么必须做：这是 140 个桶里唯一通过三判据的，多重比较下 t=2.5 达不到校正后阈值
（Bonferroni α=0.05/140 → t≈3.6）。所以不能只看总样本，必须在**每一个独立切面**
上都同向，才算不是碰巧。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
d = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_merged.parquet")
d['month'] = d['date'].str[:7]


def stat(g, label):
    if len(g) < 30:
        return
    s = g.groupby('date')['r_oc'].mean()
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan
    print(f"{label:<24}{len(g):>6}{g['r_oc'].mean():>+8.2f}%{g['r_oc'].gt(0).mean()*100:>7.1f}%"
          f"{t:>+8.2f}{len(s):>6}")


print("=" * 96)
print("全样本：new_hi60=1 vs 全市场")
print("=" * 96)
print(f"{'分组':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
stat(d[d['new_hi60'] == 1], "创60日新高")
stat(d[d['new_hi60'] == 0], "非新高")
stat(d, "全市场")

print()
print("=" * 96)
print("逐月（每个独立月份都要为正，才算稳健）")
print("=" * 96)
print(f"{'月份':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
for m, g in d[d['new_hi60'] == 1].groupby('month'):
    stat(g, m)
print(f"{'':<24}")
for m, g in d.groupby('month'):
    stat(g, f"（全市场 {m}）")

print()
print("=" * 96)
print("按新高窗口：20 / 60 / 120 / 250 日（窗口越长越难，看是否一致）")
print("=" * 96)
print(f"{'窗口':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
for w, col in ((20, 'new_hi60'), (60, 'new_hi60')):
    pass
# 20/120/250 用 ma/rolling 另算：这里用已有 hi250 与 hi60 近似
for lab, cond in [("创250日新高", d['close'] >= d['hi250'] * 0.999),
                  ("创60日新高(本信号)", d['new_hi60'] == 1)]:
    stat(d[cond.fillna(False)], lab)

print()
print("=" * 96)
print("按突破幅度：新高后离 60 日均线多远（区分「刚突破」与「已冲高」）")
print("=" * 96)
h = d[d['new_hi60'] == 1]
print(f"{'dev20 区间':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
for lo, hi in [(-99, 3), (3, 6), (6, 10), (10, 15), (15, 99)]:
    stat(h[(h['dev20'] >= lo) & (h['dev20'] < hi)], f"dev20 {lo}~{hi}%")

print()
print("=" * 96)
print("按成交额分档（流动性过滤）")
print("=" * 96)
print(f"{'成交额':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
for lo, hi in [(0, 2), (2, 5), (5, 15), (15, 999)]:
    stat(h[(h['amt_yi'] >= lo) & (h['amt_yi'] < hi)], f"{lo}~{hi} 亿")

print()
print("=" * 96)
print("按量比（突破是否放量）")
print("=" * 96)
print(f"{'volr':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
for lo, hi in [(0, 1), (1, 1.5), (1.5, 2.5), (2.5, 99)]:
    stat(h[(h['volr'] >= lo) & (h['volr'] < hi)], f"量比 {lo}~{hi}")

print()
print("=" * 96)
print("按市场环境（当日全市场涨跌中位数）")
print("=" * 96)
print(f"{'环境':<24}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}")
stat(h[h['mkt_chg'] > 0.5], "强势日(中位>+0.5%)")
stat(h[h['mkt_chg'].between(-0.5, 0.5)], "震荡日")
stat(h[h['mkt_chg'] < -0.5], "弱势日(中位<-0.5%)")

print()
print("=" * 96)
print("交易频率：每天平均几只（决定这是不是可执行策略）")
print("=" * 96)
cnt = h.groupby('date').size()
print(f"  日均有信号 {cnt.mean():.1f} 只，中位 {cnt.median():.0f}，最多 {cnt.max()}，有信号日数 {len(cnt)}/81")
