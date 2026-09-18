# -*- coding: utf-8 -*-
"""C13 预注册研究：当日一字板「日线真值 vs 池字段近似」全窗一致率。

背景（C12 §四 + 09-19 复核）：引擎 `select_limit_up` 用日线 OHLC 四价合一判当日一字
并排除（precompute `is_yizi`，precompute.py L151）；生产 `_lu_candidate` 只有
「昨日一字不接力」（yizi_prev），**无当日一字排除** → 09-14 002212（全日一字、
limit_up_time=09:25）生产选入、引擎排除——样本外生产↔引擎唯一的系统性口径差
（09-14/15 的 env/volr/概念 heat 残差经复核属 0914/0915 事故连锁，一次性）。

若要给生产补排除，hithink 池表没有可用的当日一字字段：providers.py 把「首次/最后
封板时间」映射为同一 limit_up_time（无区分力），seal/max_seal 无语义支撑。唯一
近似 = `limit_up_time <= "09:25"`（集合竞价即封）。本脚本在全窗上量化该近似与
日线真值的混淆矩阵，作为预注册判定依据——**不满足阈值则一字差异记录为已知边界，
不改生产代码**。

预注册判定（跑数前写死）：
  - FP（近似判一字、日线非一字 → 生产误伤真候选）率 < 2%（全体票占比），
    且 FN（日线一字、近似未判 → 生产漏排引擎所排之票）率 < 2% → 采纳近似；
  - 否则：近似不可用，一字口径差维持「已知边界」如实披露，C13 销项（生产零改动）。

用法：python strategy-iter/scripts/yizi_parity_study.py
输出：strategy-iter/runs/c13_yizi_parity/yizi_parity_report.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent   # strategy-iter/
sys.path.insert(0, str(BASE))
from engine.data import SEL_START, SEL_END, build_store   # noqa: E402

OUT = BASE / "runs" / "c13_yizi_parity"


def engine_truth(store) -> pd.DataFrame:
    """全窗 [SEL_START, SEL_END] 涨停池票的日线一字真值（引擎同式：raw OHLC 四价合一）。"""
    pool = store.pool.copy()
    rawm = store.raw[["thscode", "date", "open", "high", "low", "close"]].rename(
        columns={"open": "r_open", "high": "r_high", "low": "r_low", "close": "r_close"})
    pool = pool.merge(rawm, on=["thscode", "date"], how="left")
    pool = pool[(pool["date"] >= SEL_START) & (pool["date"] <= SEL_END)]
    pool["yizi_true"] = ((pool["r_open"] == pool["r_high"])
                         & (pool["r_open"] == pool["r_low"])
                         & (pool["r_open"] == pool["r_close"]))
    return pool[["date", "thscode", "lu_time", "yizi_true"]]


def proxy(df: pd.DataFrame, cut: str) -> pd.Series:
    return df["lu_time"].astype(str) <= cut


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    store = build_store()
    df = engine_truth(store)
    n_all = len(df)
    n_true = int(df["yizi_true"].sum())
    lines = [
        "# C13 一字板近似判据一致率（日线真值 vs limit_up_time 阈值）",
        "",
        f"- 窗口 {SEL_START}..{SEL_END} · 涨停池票 {n_all} · 日线真一字 {n_true}"
        f"（{100.0 * n_true / n_all:.2f}%）",
        f"- 预注册判定：FP 率 <2% 且 FN 率 <2%（占全体票）→ 采纳近似；否则销项零改动",
        "",
        "| 阈值 | 近似判一字 | TP | FP(误伤) | FN(漏排) | FP率 | FN率 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    adopted = None
    for cut in ("09:25", "09:30", "09:31", "10:00"):
        y = proxy(df, cut)
        tp = int((y & df["yizi_true"]).sum())
        fp = int((y & ~df["yizi_true"]).sum())
        fn = int((~y & df["yizi_true"]).sum())
        fp_rate, fn_rate = 100.0 * fp / n_all, 100.0 * fn / n_all
        ok = fp_rate < 2.0 and fn_rate < 2.0
        lines.append(f"| ≤{cut} | {int(y.sum())} | {tp} | {fp} | {fn} |"
                     f" {fp_rate:.2f}% | {fn_rate:.2f}% | {'✅采纳候选' if ok else '✗'} |")
        if ok and adopted is None:
            adopted = cut
    # FP/FN 明细（辅助人工复核）
    y25 = proxy(df, "09:25")
    fp_rows = df[y25 & ~df["yizi_true"]]
    fn_rows = df[~y25 & df["yizi_true"]]
    lines += ["", "## ≤09:25 的 FP 明细（近似误伤样本，最多列 15 条）", "",
              "| 日期 | 代码 | lu_time |", "|---|---|---|"]
    for r in fp_rows.head(15).itertuples():
        lines.append(f"| {r.date} | {r.thscode} | {r.lu_time} |")
    lines += ["", "## ≤09:25 的 FN 明细（近似漏排样本，最多列 15 条）", "",
              "| 日期 | 代码 | lu_time |", "|---|---|---|"]
    for r in fn_rows.head(15).itertuples():
        lines.append(f"| {r.date} | {r.thscode} | {r.lu_time} |")
    lines += ["", "## 判定", "",
              (f"**采纳 ≤{adopted}**：满足 FP/FN 双阈值 → 生产 `_lu_candidate` 增加"
               f"「当日一字排除（lu_time≤{adopted} 近似）」，与引擎日线真值在阈值内等价。"
               if adopted is not None else
               "**所有阈值均不满足预注册判定 → 近似不可用，一字口径差维持「已知边界」披露，"
               "生产零改动，C13 销项。"), ""]
    txt = "\n".join(lines)
    (OUT / "yizi_parity_report.md").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
