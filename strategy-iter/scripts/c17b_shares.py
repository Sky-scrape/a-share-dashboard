# -*- coding: utf-8 -*-
"""C17b 第 1 步：补流通股本，算真实换手率与流通市值。

来源：akshare `stock_zh_a_spot_em()`（东财全市场快照，含「流通市值」「总市值」「换手率」）。
本地库没有股本，这里用 快照流通市值 / 快照最新价 = 流通股本，再用历史成交量 /
该股本得到**历史换手率近似**。

近似边界（必须披露）：股本在窗口内可能变动（增发、解禁、回购），用 11-03 的股本
近似 7-10 月，对多数票误差有限，对期间有股本变动的票会偏高或偏低。字段名带 approx。
"""
import json
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c17_shape"
snap_file = OUT / "spot_20261103.parquet"

if not snap_file.exists():
    print("抓取东财全市场快照…")
    spot = ak.stock_zh_a_spot_em()
    spot = spot[['代码', '名称', '最新价', '成交额', '换手率', '总市值', '流通市值', '市盈率-动态']]
    spot.to_parquet(snap_file, index=False)
    print("已存", snap_file, spot.shape)
else:
    spot = pd.read_parquet(snap_file)
    print("复用快照", spot.shape)

spot['thscode'] = spot['代码'].astype(str).str.zfill(6)
spot['float_share'] = spot['流通市值'] / spot['最新价']          # 股
spot['total_share'] = spot['总市值'] / spot['最新价']
print(spot[['名称', '最新价', '流通市值', 'float_share', '换手率']].head(3).to_string())
print(f"有效股本样本 {spot['float_share'].notna().sum()} / {len(spot)}")

# ---- 重算历史换手率与流通市值 ----
q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
share = spot.set_index('thscode')['float_share'].to_dict()
q['float_share'] = q['thscode'].map(share)
# 成交量单位：本地库 volume 为股；换手率 = volume / 流通股本 * 100
q['turn'] = q['volume'] / q['float_share'] * 100
q['cap'] = q['close'] * q['float_share'] / 1e8            # 流通市值（亿元）
q['turn'] = q['turn'].clip(upper=100)

sub = q[(q['date'] >= '2026-07-01') & (q['date'] <= '2026-10-30')]
print(f"\n7-10 月样本 {len(sub)}，换手率有效 {sub['turn'].notna().sum()}")
print(sub['turn'].describe(percentiles=[.1, .5, .9]).round(2).to_string())
print(f"流通市值（亿）分位：{sub['cap'].quantile([.1,.25,.5,.75,.9]).round(1).to_dict()}")

# 抽查：用换手率反算东财快照的换手率是否对得上（最后一日）
last = sub[sub['date'] == sub['date'].max()][['thscode', 'turn', 'cap']]
chk = last.merge(spot[['thscode', '名称', '换手率', '流通市值']], on='thscode')
chk['cap_em'] = chk['流通市值'] / 1e8
ok = chk.dropna(subset=['turn', '换手率'])
print(f"\n校验（{ok.shape[0]} 只）：换手率相关性 "
      f"{ok['turn'].corr(ok['换手率'].astype(float)):.4f}，"
      f"流通市值相关性 {ok['cap'].corr(ok['cap_em']):.4f}")

q[['thscode', 'date', 'turn', 'cap']].to_parquet(OUT / "turn_cap.parquet", index=False)
spot[['thscode', '名称', 'float_share', 'total_share']].to_parquet(
    OUT / "shares.parquet", index=False)
print("\n写入 turn_cap.parquet / shares.parquet")
