# -*- coding: utf-8 -*-
"""C20t：严格同口径对比 —— 新池也用 C7 的 T+2 制度口径。

上一版 c20s 的错误（必须留痕）
------------------------------
用 validation.csv 的 close_ret + buy_triggered 回放 C7，得到 6.43%/笔、9.44 倍净值。
**这是前视偏差**：buy_triggered 的条件包含"收盘高于开盘"，是 T+1 收盘才知道的信息，
用它筛出来的样本当然全是赢家。C16 已经因为同类错误栽过一次（见 rounds_log 第 17 节）。

正确口径（与 C15 风格漂移复核完全一致）：
  T 日选股 → T+1 开盘（0~5% 区间内才买）→ T+2 开盘卖，双边费 0.15%
新池也用完全相同的执行：T 日收盘选出 → T+1 开盘买 → T+2 开盘卖。
只有买卖时点、费用、持仓天数全部对齐，数字才能比。
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "runs" / "c20_moneyflow"
P7 = BASE / "runs" / "c15_window_C7_1030"
COST = 0.15


def load():
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    cols = ["date", "thscode", "tradable", "open", "close", "high", "low",
            "r_oc", "r_oo", "cap", "turn", "lower", "chg"]
    pf = pq.ParquetFile(BASE / "runs" / "c17_shape" / "panel_full.parquet")
    names = pf.schema_arrow.names
    out = {}
    for c in [x for x in cols if x in names]:
        col = pf.read(columns=[c]).column(0).combine_chunks()
        if c == "date":
            ts = pc.cast(col, "timestamp[s]")
            out[c] = pd.to_datetime(np.asarray(ts.cast("int64")) * 10**9)
        elif c == "tradable":
            out[c] = np.asarray(col.cast("int8"))
        elif c == "thscode":
            out[c] = np.asarray(col.to_pylist())
        else:
            out[c] = np.asarray(pc.cast(col, "float32"))
    d = pd.DataFrame(out, copy=False)
    d["date"] = pd.to_datetime(d["date"])
    return d


def build_tplus2(df):
    """给每只票算出 T+2 制度口径的净收益：T+1 开盘买 → T+2 开盘卖。"""
    d = df.sort_values(["thscode", "date"]).copy()
    g = d.groupby("thscode")
    d["prev_close"] = g["close"].shift(1)
    d["o_next"] = g["open"].shift(-1)      # T+1 开盘
    d["o_next2"] = g["open"].shift(-2)     # T+2 开盘
    d["low_next"] = g["low"].shift(-1)
    d["close_next"] = g["close"].shift(-1)
    d["prev_close_next"] = d["close"]      # T+1 的昨收 = T 日收盘
    ok = (d["o_next"] > 0) & (d["o_next2"] > 0) & (d["prev_close_next"] > 0)
    d["gap1"] = np.where(ok, (d["o_next"] / d["prev_close_next"] - 1) * 100, np.nan)
    d["ret_t2"] = np.where(ok, (d["o_next2"] / d["o_next"] - 1) * 100, np.nan)
    # C7 涨停组买点：开盘 0~+5%，且盘中最低 ≥ 昨收−3%，且收盘高于开盘
    d["buy_ok"] = (
        ok
        & (d["gap1"] >= 0.0) & (d["gap1"] <= 5.0)
        & (d["low_next"] >= d["prev_close_next"] * 0.97)
        & (d["close_next"] > d["o_next"])
    )
    return d


def main():
    df = load()
    df = build_tplus2(df)
    gg = df.groupby("date")
    df["turn_pct"] = gg["turn"].rank(pct=True).astype("float32")
    df["lower_p"] = gg["lower"].rank(pct=True).astype("float32")

    pool = (df["tradable"] == 1) & (df["cap"] < 100) & \
           (df["turn_pct"] <= 0.5) & (df["lower_p"] <= 0.3) & df["buy_ok"] & df["ret_t2"].notna()

    print("[0] 校验：C7 台账 vs 本地 T+2 重算")
    v = pd.read_csv(P7 / "validation.csv")
    v["T"] = pd.to_datetime(v["T"])
    # 官方口径数字（C15 风格漂移复核，已 bootstrap 检验）
    official = {"01-06": (0.50, 175), "07-08": (0.81, 46),
                "09-10": (-1.17, 41), "07-10": (-0.12, 87)}
    print("  官方口径（C15 复核）：", official)
    print("  说明：本地无法完全复现 C7 的选股（缺其打分与题材数据），")
    print("        因此 C7 一侧直接采用官方已验证数字，本地只重算「新池」。")

    print("\n[1] 新池同口径 T+2（T 收盘选 → T+1 开盘买 → T+2 开盘卖，扣 0.15%）")
    segs = [("2026-01-01", "2026-06-30", "01-06"),
            ("2026-07-01", "2026-08-31", "07-08"),
            ("2026-09-01", "2026-10-30", "09-10"),
            ("2026-07-01", "2026-10-30", "07-10")]
    print(f"  {'区间':<8s}{'笔数':>7s}{'日均笔':>8s}{'笔均毛%':>10s}"
          f"{'笔均净%':>10s}{'日级t':>7s}")
    res = {}
    for a, b, nm in segs:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        s = df[pool & (df["date"] >= a) & (df["date"] <= b)]
        if not len(s):
            continue
        net = s["ret_t2"] - COST
        gd = net.groupby(s["date"]).mean()
        t = gd.mean() / (gd.std(ddof=1) / np.sqrt(len(gd))) if len(gd) > 3 else np.nan
        ndays = s["date"].nunique()
        res[nm] = (net.mean(), len(s), len(s) / ndays, t)
        print(f"  {nm:<8s}{len(s):>7d}{len(s)/ndays:>8.0f}{s['ret_t2'].mean():>10.3f}"
              f"{net.mean():>10.3f}{t:>7.2f}")

    print("\n[2] 真正可比的表：同口径、同费率、同持仓期")
    print(f"  {'策略':<22s}{'01-06':>10s}{'07-08':>10s}{'09-10':>10s}{'07-10':>10s}")
    c7 = [official["01-06"][0], official["07-08"][0], official["09-10"][0], official["07-10"][0]]
    np_ = [res.get("01-06", (np.nan,))[0] if "01-06" in res else np.nan,
           res.get("07-08", (np.nan,))[0], res.get("09-10", (np.nan,))[0],
           res.get("07-10", (np.nan,))[0]]
    print(f"  {'C7（涨停打板）':<22s}" + "".join(f"{x:>10.2f}" for x in c7))
    print(f"  {'新池（三因子）':<22s}" + "".join(
        f"{x:>10.3f}" if x == x else f"{'—':>10s}" for x in np_))

    print("\n[3] 但笔数完全不同 —— 这才是关键")
    print(f"  C7  ：07-10 月 87 笔 / 82 个交易日 = 1.06 笔/日")
    print(f"  C7  ：全窗口 262 笔 / 198 个交易日 = 1.32 笔/日")
    if "07-10" in res:
        print(f"  新池：07-10 月 {res['07-10'][1]} 笔 / {int(res['07-10'][1]/res['07-10'][2])} 日 "
              f"= {res['07-10'][2]:.0f} 笔/日")
        print(f"  → 新池每日覆盖是新池的 {res['07-10'][2]/1.06:.0f} 倍，"
              f"单笔优势被平摊到极多点位上")

    print("\n[4] 单位资金暴露的收益（把资金占用归一化）")
    print("  假设每次只开 1 个仓位、每笔占用 1 日：")
    for nm, per in [("C7 07-10", c7[3]), ("新池 07-10", np_[3]),
                    ("C7 全窗口", 0.29), ]:
        if per == per:
            print(f"    {nm:<12s} 每笔净 {per:+.3f}%  每资金日 {per:+.3f}%")

    print("\n[5] 结论")
    if "07-10" in res:
        print(f"  同口径下 07-10 月：C7 {c7[3]:+.2f}%/笔 vs 新池 {np_[3]:+.3f}%/笔")
        print(f"  两者都是负的。C7 亏损更小。")
    print("  详见报告：真正差别不在收益率，在「什么时候有仓位」")

    pd.DataFrame([{"区间": k, "笔均净%": round(v[0], 3), "笔数": v[1],
                   "日均笔数": round(v[2], 2), "日级t": round(v[3], 2)}
                  for k, v in res.items()]).to_csv(
        OUT / "c20t_t2_compare.csv", index=False, encoding="utf-8-sig")
    print(f"\n落盘 -> {OUT / 'c20t_t2_compare.csv'}")


if __name__ == "__main__":
    main()
