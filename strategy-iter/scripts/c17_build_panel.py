# -*- coding: utf-8 -*-
"""C17 第 1 步：全市场日线特征面板（形态 / 量价 / 筹码近似 / 趋势位置）。

数据边界诚实声明：本地库只有 OHLCV + amount，**没有股本、没有 Level2、没有筹码分布**。
所以「换手率」用成交额的自历史分位代理，「筹码结构」用成交量加权成本分布近似
（VWAP 与上方套牢盘），都在字段名里标明 approx。不用任何未来数据。

交易模式：**T 日收盘买入 → T+1 收盘卖出**（唯一基线未被证明为负的模式：
全市场 −0.247%、日级 t=−1.6）。
"""
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"
OUT.mkdir(parents=True, exist_ok=True)
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")

q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
q = q[q['thscode'].str[:3].isin(MAIN)].copy()
q = q[q['date'] >= "2025-08-01"].sort_values(['thscode', 'date']).reset_index(drop=True)
print("原始行", len(q))

g = q.groupby('thscode', sort=False)
c, o, h, l, v, a = q['close'], q['open'], q['high'], q['low'], q['volume'], q['amount']

# ---- 基础形态 ----
rng = (h - l).replace(0, np.nan)
q['body'] = (c - o) / rng                       # 实体占比（带符号）
q['upper'] = (h - np.maximum(o, c)) / rng       # 上影线占比
q['lower'] = (np.minimum(o, c) - l) / rng       # 下影线占比
q['gap'] = (o / g['close'].shift(1) - 1) * 100  # 跳空
q['chg'] = g['close'].pct_change() * 100
q['amp'] = rng / g['close'].shift(1) * 100

# ---- 趋势 / 位置 ----
for n in (5, 10, 20, 60):
    q[f'ma{n}'] = g['close'].transform(lambda s, n=n: s.rolling(n).mean())
q['ma_bull'] = ((q['ma5'] > q['ma10']) & (q['ma10'] > q['ma20'])).astype(float)
q['dev20'] = (c / q['ma20'] - 1) * 100
q['dev60'] = (c / q['ma60'] - 1) * 100
q['ret20'] = g['close'].pct_change(20) * 100
q['ret60'] = g['close'].pct_change(60) * 100
q['hi60'] = g['high'].transform(lambda s: s.rolling(60).max())
q['hi250'] = g['high'].transform(lambda s: s.rolling(250).max())
q['lo60'] = g['low'].transform(lambda s: s.rolling(60).min())
q['pos60'] = (c - q['lo60']) / (q['hi60'] - q['lo60']).replace(0, np.nan)  # 0=箱底 1=箱顶
q['to_hi60'] = (q['hi60'] / c - 1) * 100         # 距 60 日高点还有多少空间
q['new_hi60'] = (c >= q['hi60'] * 0.999).astype(float)

# ---- 量能 ----
q['vol_ma5'] = g['volume'].transform(lambda s: s.rolling(5).mean())
q['vol_ma20'] = g['volume'].transform(lambda s: s.rolling(20).mean())
q['volr'] = v / q['vol_ma5']                     # 量比
q['vol_trend'] = q['vol_ma5'] / q['vol_ma20']    # 量能趋势
q['amt_rank60'] = a.groupby(q['thscode']).transform(
    lambda s: s.rolling(60).apply(lambda x: (x[-1] >= x).mean(), raw=True))  # 成交额自历史分位
q['amt_yi'] = a / 1e8
q['vol_price_div'] = ((q['volr'] > 1.5) & (q['chg'] < 0)).astype(float)   # 放量下跌
q['vol_up'] = ((q['volr'] > 1.5) & (q['chg'] > 3)).astype(float)          # 放量上攻
q['shrink_pull'] = ((q['volr'] < 0.8) & (q['chg'].abs() < 2)
                    & (q['dev20'].abs() < 4)).astype(float)               # 缩量回踩

# ---- 筹码近似（成交量加权成本）----
tp = (h + l + c) / 3
qv = (tp * v)
for n in (20, 60):
    vw = qv.groupby(q['thscode']).transform(lambda s, n=n: s.rolling(n).sum())
    vv = v.groupby(q['thscode']).transform(lambda s, n=n: s.rolling(n).sum())
    q[f'vwap{n}'] = vw / vv.replace(0, np.nan)
q['profit20'] = (c / q['vwap20'] - 1) * 100      # >0 = 近20日买入者平均浮盈
q['profit60'] = (c / q['vwap60'] - 1) * 100
# 上方套牢盘：近 60 日成交量里，典型价高于现价的比例（筹码分散度代理）
q['above_ratio'] = np.nan


def _above(df):
    p = df['close'].to_numpy()
    t = ((df['high'] + df['low'] + df['close']) / 3).to_numpy()
    vv = df['volume'].to_numpy()
    out = np.full(len(df), np.nan)
    win = 60
    for i in range(win - 1, len(df)):
        sl = slice(i - win + 1, i + 1)
        vw = vv[sl].sum()
        if vw > 0:
            out[i] = vv[sl][t[sl] > p[i]].sum() / vw
    return pd.Series(out, index=df.index)


q['above_ratio'] = q.groupby('thscode', group_keys=False).apply(_above, include_groups=False)
# 成本集中度：近 60 日加权成本的标准差 / 均价（小=筹码集中）
q['cost_disp'] = (tp - q['vwap60']).abs().groupby(q['thscode']).transform(
    lambda s: s.rolling(60).mean()) / q['vwap60'].replace(0, np.nan) * 100

# ---- 波动 ----
q['atr'] = (h - l).groupby(q['thscode']).transform(lambda s: s.rolling(20).mean())
q['atr_pct'] = q['atr'] / c * 100

# ---- 相对强度（vs 全市场当日中位数）----
q['mkt_chg'] = q.groupby('date')['chg'].transform('median')
q['rel_str'] = q['chg'] - q['mkt_chg']

q = q[q['date'] >= "2026-07-01"].copy()
td = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
nxt = {td[i]: td[i + 1] for i in range(len(td) - 1)}
q['T1'] = q['date'].map(nxt)
q = q[q['T1'].notna()]

qi = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
qi['date'] = pd.to_datetime(qi['date']).dt.strftime("%Y-%m-%d")
qi = qi.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])


def fetch(code, date, col):
    return qi[col].reindex(pd.MultiIndex.from_arrays([code, date])).to_numpy()


q['o1'] = fetch(q['thscode'], q['T1'], 'open')
q['c1'] = fetch(q['thscode'], q['T1'], 'close')
q = q.dropna(subset=['o1', 'c1']).reset_index(drop=True)

FEE = 0.15
q['r_oo'] = (q['o1'] / q['close'] - 1) * 100 - FEE        # T收买 → T+1开卖
q['r_oc'] = (q['c1'] / q['close'] - 1) * 100 - FEE        # T收买 → T+1收卖（主口径）
q['overnight'] = (q['o1'] / q['close'] - 1) * 100
q['intraday'] = (q['c1'] / q['o1'] - 1) * 100
q['seg'] = np.where(q['date'] < '2026-09-01', 'train', 'test')

q.to_parquet(OUT / "panel_0710.parquet", index=False)
print("面板行", len(q), " 日期", q['date'].min(), "~", q['date'].max())
print(f"基线（T收买→T+1收卖）：全 {q['r_oc'].mean():+.3f}%  "
      f"train {q[q.seg=='train']['r_oc'].mean():+.3f}%  test {q[q.seg=='test']['r_oc'].mean():+.3f}%")
print("特征列:", [x for x in q.columns if x not in
                ('thscode', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                 'prev_close', 'T1', 'o1', 'c1', 'r_oo', 'r_oc', 'overnight', 'intraday', 'seg')])
print("写入", OUT / "panel_0710.parquet")
