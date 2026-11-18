# -*- coding: utf-8 -*-
"""C17 第 5 步：把「创新高 + 缩量 + 小成交额」拼成策略，做严格分段与逐月验证。

诚实提示：这三个条件是**在同一批数据上逐层细分**出来的（先发现新高，再在新高里发现
缩量和小盘），属于二次挖掘。因此判据必须更严：训练段（7-8 月）与验证段（9-10 月）
分别独立算，任何一段不达标就不采用。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
d = pd.read_parquet(BASE / "runs" / "c17_shape" / "panel_merged.parquet")
d['month'] = d['date'].str[:7]
TR, TE = d['seg'] == 'train', d['seg'] == 'test'


def stat(g, label, width=26):
    if len(g) < 20:
        print(f"{label:<{width}}{len(g):>6}   样本不足")
        return
    s = g.groupby('date')['r_oc'].mean()
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan
    print(f"{label:<{width}}{len(g):>6}{g['r_oc'].mean():>+8.2f}%{g['r_oc'].gt(0).mean()*100:>7.1f}%"
          f"{t:>+8.2f}{len(s):>6}{g.groupby('date').size().mean():>7.1f}")


SIG = {
    "A 仅创新高":            d['new_hi60'] == 1,
    "B 新高+缩量(volr<1)":  (d['new_hi60'] == 1) & (d['volr'] < 1),
    "C 新高+小额(<5亿)":     (d['new_hi60'] == 1) & (d['amt_yi'] < 5),
    "D 新高+缩量+小额":      (d['new_hi60'] == 1) & (d['volr'] < 1) & (d['amt_yi'] < 5),
    "E D+远离均线(dev20>10)": (d['new_hi60'] == 1) & (d['volr'] < 1) & (d['amt_yi'] < 5) & (d['dev20'] > 10),
}

hdr = f"{'策略':<26}{'n':>6}{'净均':>9}{'胜率':>8}{'日级t':>8}{'日数':>6}{'日均只数':>8}"
print("=" * 106)
print("分段验证（训练 7-8 月 / 验证 9-10 月，各自独立）")
print("=" * 106)
for name, m in SIG.items():
    m = m.fillna(False)
    print(f"\n【{name}】")
    print(hdr)
    stat(d[m & TR], "  训练段")
    stat(d[m & TE], "  验证段")
    stat(d[m], "  全窗口")

print()
print("=" * 106)
print("策略 D（新高+缩量+小额）逐月")
print("=" * 106)
m = SIG["D 新高+缩量+小额"].fillna(False)
print(hdr)
for mo, g in d[m].groupby('month'):
    stat(g, f"  {mo}")
print(hdr)
for mo, g in d.groupby('month'):
    stat(g, f"  （全市场 {mo}）")

print()
print("=" * 106)
print("策略 D 在不同离场时点（同一批票，只改卖出时间）")
print("=" * 106)
g = d[m]
for col, lab in (('r_oc', 'T+1 收盘卖（主口径）'), ('r_oo', 'T+1 开盘卖')):
    s = g.groupby('date')[col].mean()
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))
    print(f"  {lab:<22} 净均 {g[col].mean():+.2f}%  日级t {t:+.2f}")
print(f"  隔夜 {g['overnight'].mean():+.2f}%   日内(T+1开→收) {g['intraday'].mean():+.2f}%")

print()
print("=" * 106)
print("策略 D 的成交额分布（流动性风险检查：能买多少）")
print("=" * 106)
print(g['amt_yi'].describe(percentiles=[.1, .25, .5, .75, .9]).round(2).to_string())
print(f"\n  日均有信号 {g.groupby('date').size().mean():.1f} 只；"
      f"单票日均成交额中位 {g['amt_yi'].median():.1f} 亿")
print(f"  若每日买 2 只、每只 20 万，占用当日成交额约 "
      f"{20/ (g['amt_yi'].median()*1e4) *100:.2f}%（冲击可控）")
