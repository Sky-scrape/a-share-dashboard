# -*- coding: utf-8 -*-
"""C16：只用 2026-07-01 ~ 10-30 数据重建策略——第 1 步，建样本集。

不沿用 C7 的打分体系，从全量涨停池出发，先看清楚「哪些票在 T+1 买入、T+2 卖出
能赚钱」。样本基数 ~5000 只次，比 C7 生产池（84 天 190 笔）大一个量级，
这是唯一可能做出统计结论的前提。

口径（与 production spec_exec_t2 一致）：
- 建仓：T+1 开盘，且开盘涨幅落在 [0%, +5%]（涨停板次日买点窗，同 execution_layer）
- 离场：T+2 开盘（主口径）/ T+2 收盘（对照）
- 费：双边 0.15%
- 收益按「相对 T+1 开盘建仓价」折算
"""
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c16_new_strategy"
OUT.mkdir(parents=True, exist_ok=True)
START, END = "2026-07-01", "2026-10-30"
FEE = 0.15
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")


def load_pool():
    rows = []
    for f in sorted(glob.glob(str(BASE / "raw" / "pool" / "lu_*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        t = d.get("trade_date")
        if not t or t < START or t > END:
            continue
        for it in d.get("items") or []:
            rows.append(dict(
                T=t, thscode=it.get("thscode"), name=it.get("name"),
                pct=it.get("price_change_ratio_pct"),
                lu_time=it.get("limit_up_time"), cnt=it.get("continue_day_cnt"),
                seal=it.get("seal_money"), max_seal=it.get("max_seal_money"),
                open_times=it.get("open_times"), is_st=bool(it.get("is_st")),
                is_new=bool(it.get("is_new")), reason=it.get("limit_up_reason") or "",
            ))
    return pd.DataFrame(rows)


def main():
    p = load_pool()
    print("涨停池（7-10 月）", p.shape, "日期数", p['T'].nunique())
    p = p[p['thscode'].str[:3].isin(MAIN)]          # 主板（与 C7 同口径）
    p = p[~p['is_st'] & ~p['is_new']]
    print("主板非ST非次新", p.shape, "日期数", p['T'].nunique())

    q = pd.read_parquet(BASE / "data" / "daily_qfq.parquet")
    q['date'] = pd.to_datetime(q['date']).dt.strftime("%Y-%m-%d")
    r = pd.read_parquet(BASE / "data" / "daily_raw.parquet")
    r['date'] = pd.to_datetime(r['date']).dt.strftime("%Y-%m-%d")
    tdays = (BASE / "data" / "trade_dates.txt").read_text(encoding="utf-8").split("\n")
    nxt = {tdays[i]: tdays[i + 1] for i in range(len(tdays) - 1)}
    nxt2 = {tdays[i]: tdays[i + 2] for i in range(len(tdays) - 2)}

    p['T1'] = p['T'].map(nxt)
    p['T2'] = p['T'].map(nxt2)
    p = p[p['T1'].notna() & p['T2'].notna()].reset_index(drop=True)

    qi = q.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])[
        ['open', 'high', 'low', 'close']]
    for tag, col in (("0", "T"), ("1", "T1"), ("2", "T2")):
        sub = qi.reindex(pd.MultiIndex.from_arrays([p['thscode'], p[col]]))
        for k in ('open', 'high', 'low', 'close'):
            p[f"{tag}{k[0]}"] = sub[k].to_numpy()

    ri = r.drop_duplicates(['thscode', 'date']).set_index(['thscode', 'date'])['amount']
    p['amt0'] = ri.reindex(pd.MultiIndex.from_arrays([p['thscode'], p['T']])).to_numpy()

    p = p.dropna(subset=['0c', '1o', '1c', '2o', '2c'])
    # 涨幅口径（相对 T 收盘）
    p['open_ret'] = (p['1o'] / p['0c'] - 1) * 100
    p['close_ret'] = (p['1c'] / p['0c'] - 1) * 100
    p['fillable'] = (p['open_ret'] >= 0) & (p['open_ret'] <= 5)
    # T+1 建仓 → T+2 开盘卖
    p['net_open'] = (p['2o'] / p['1o'] - 1) * 100 - FEE
    p['net_close'] = (p['2c'] / p['1o'] - 1) * 100 - FEE
    # 分解
    p['intraday_t1'] = (p['1c'] / p['1o'] - 1) * 100
    p['overnight'] = (p['2o'] / p['1c'] - 1) * 100
    p['intraday_t2'] = (p['2c'] / p['2o'] - 1) * 100
    # 特征
    p['seal_ratio'] = np.where(p['amt0'] > 0, p['seal'] / p['amt0'], np.nan)
    p['max_seal_ratio'] = np.where(p['amt0'] > 0, p['max_seal'] / p['amt0'], np.nan)
    p['early'] = p['lu_time'].fillna("99:99") <= "10:00"
    p['late'] = p['lu_time'].fillna("00:00") >= "13:30"
    p['amt_yi'] = p['amt0'] / 1e8
    p['reopen'] = (p['open_times'].fillna(0) > 0)
    p['month'] = p['T'].str[:7]

    # 位置：60 日涨幅（相对 T 收盘的前 60 个交易日）
    px = q[q['date'] <= END].sort_values(['thscode', 'date'])
    g = px.groupby('thscode')['close']
    px['ret60'] = (px['close'] / g.shift(60) - 1) * 100
    px['ret20'] = (px['close'] / g.shift(20) - 1) * 100
    p = p.merge(px[['thscode', 'date', 'ret60', 'ret20']].rename(columns={'date': 'T'}),
                on=['thscode', 'T'], how='left')

    # 市场环境：当日全市场涨停家数、主板涨停家数
    mk = p.groupby('T').size().rename('lu_cnt_main').reset_index()
    p = p.merge(mk, on='T', how='left')
    p['lu_cnt_main_ma3'] = p.groupby('T')['lu_cnt_main'].transform('first')
    tmp = p[['T', 'lu_cnt_main']].drop_duplicates().sort_values('T')
    tmp['lu_ma3'] = tmp['lu_cnt_main'].rolling(3).mean()
    p = p.drop(columns=['lu_cnt_main_ma3']).merge(tmp[['T', 'lu_ma3']], on='T', how='left')
    p['lu_expand'] = p['lu_cnt_main'] > p['lu_ma3'] * 1.1

    p.to_parquet(OUT / "samples_0710.parquet", index=False)
    print("样本行", len(p), " 可成交", int(p['fillable'].sum()))
    print("日期", p['T'].min(), p['T'].max(), " 天数", p['T'].nunique())
    print(p.groupby('month').agg(n=('thscode', 'size'),
                                 fill=('fillable', 'sum'),
                                 net=('net_open', 'mean')).round(2))
    print("\n可成交子集：")
    f = p[p['fillable']]
    print(f"  n={len(f)}  T+2开盘卖均={f['net_open'].mean():+.2f}%  T+2收盘卖均={f['net_close'].mean():+.2f}%")
    print(f"  日内(T1建仓→收)={f['intraday_t1'].mean():+.2f}%  隔夜={f['overnight'].mean():+.2f}%  T+2日内={f['intraday_t2'].mean():+.2f}%")
    print(f"\n写入 {OUT / 'samples_0710.parquet'}")


if __name__ == "__main__":
    main()
