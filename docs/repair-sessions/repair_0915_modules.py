# -*- coding: utf-8 -*-
"""20260915 快照五模块合并式回填（key 丢失事故恢复，跑完即删）。

可回补：breadth(池子精确+涨跌家数日线近似)/concepts(index history 390 只)/
extra(分布+TOP20 从研究库)/hot_stock(hot-stock-history)/etf(fund history)。
不可回补部分如实置空（换手率/PE/PB/ROE/大小盘），并加 repair_note 标注近似口径。
绝不触碰 speculation/pool 与已 ok 模块。
"""
import datetime
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "backend")
sys.path.insert(0, "backend/recap")
import ht  # noqa: E402
import providers  # noqa: E402
import fsutil  # noqa: E402
import spec_duckdb  # noqa: E402

DATE = "20260915"
D_ISO = "2026-09-15"
SNAP = os.path.join("data", "recap", f"{DATE}.json")

snap = json.load(open(SNAP, encoding="utf-8"))
results = {}


# ---------- 研究库日线：全市场 09-15 涨跌幅/成交额 ----------
def market_rows():
    rows = spec_duckdb._db_export(
        "SELECT a.thscode AS code, a.close AS close, a.amount AS amount, "
        "b.close AS prev_close "
        "FROM v_daily_qfq a JOIN v_daily_qfq b ON a.thscode = b.thscode "
        "AND b.date = DATE '2026-09-11' "
        "WHERE a.date = DATE '" + D_ISO + "' AND b.close > 0", "repair0915_all")
    out = []
    for r in rows or []:
        try:
            pct = (float(r["close"]) / float(r["prev_close"]) - 1) * 100.0
        except Exception:  # noqa: BLE001
            continue
        out.append({"code": str(r["code"]), "pct": pct,
                    "close": float(r["close"]), "amount": float(r["amount"] or 0)})
    return out


mkt = market_rows()
print(f"[1/5] 研究库 09-15 全市场 {len(mkt)} 只")
pcts = [r["pct"] for r in mkt]

def _hist_bars(cmd, code):
    """index/fund history 通用取数：字段名 close_price/date_ms/turnover，
    返回 [(本地日期, close, turnover)] 升序。"""
    start_ms = int(datetime.datetime(2026, 9, 8).timestamp() * 1000)
    end_ms = int(datetime.datetime(2026, 9, 16).timestamp() * 1000)
    d = ht.ht(cmd[0], cmd[1], "--thscode", code,
              "--start-ms", str(start_ms), "--end-ms", str(end_ms))
    out = []
    for x in (d.get("item") or []):
        try:
            day = datetime.datetime.fromtimestamp(
                float(x["date_ms"]) / 1000).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        if x.get("close_price") is not None:
            out.append((day, float(x["close_price"]), float(x.get("turnover") or 0)))
    return sorted(out)


# ---------- breadth ----------
def do_breadth():
    zt = ((snap["modules"].get("limit_up_pool") or {}).get("data") or [])
    dt = ((snap["modules"].get("limit_down_pool") or {}).get("data") or [])
    zb = ((snap["modules"].get("limit_break_pool") or {}).get("data") or [])
    lbs = [int(r.get("连板数") or 0) for r in zt]
    data = {
        "上涨": sum(1 for p in pcts if p > 0),
        "下跌": sum(1 for p in pcts if p < 0),
        "平盘": sum(1 for p in pcts if p == 0),
        "涨停家数": len(zt), "跌停家数": len(dt),
        "最高连板": max(lbs) if lbs else 0,
        "炸板家数": len(zb),
        "真实涨停": len(zt),   # 近似口径：ST/新股区分字段在快照行缺失，如实从简
    }
    # 两市成交额：hithink 指数日线（沪 000001.SH + 深 399106.SZ）
    try:
        amt = 0.0
        for code in ("000001.SH", "399106.SZ"):
            bars = _hist_bars(("index", "history"), code)
            for day, _c, turnover in bars:
                if day == D_ISO:
                    amt += turnover
        if amt > 0:
            data["两市成交额"] = amt
    except Exception as e:  # noqa: BLE001
        print("    两市成交额跳过:", type(e).__name__)
    results["breadth"] = {"status": "ok", "data": data,
                          "note": "涨跌家数/真实涨停为日线近似口径（0916 key 事故回填）"}
    print("[2/5] breadth:", data)


# ---------- extra ----------
PCT_BINS = [("涨停", 9.9, 999), ("涨停-5%", 5, 9.9), ("5-1%", 1, 5),
            ("1-0%", 1e-6, 1), ("平盘", 0, 0), ("0-1%", -1, 0),
            ("1-5%", -5, -1), ("5%-跌停", -9.9, -5), ("跌停", -999, -9.9)]


def do_extra():
    dist = []
    for label, lo, hi in PCT_BINS:
        if lo == hi:
            n = sum(1 for p in pcts if p == 0)
        else:
            n = sum(1 for p in pcts if lo <= p < hi)
        dist.append({"区间": label, "家数": n})
    try:
        name_map = providers._symbol_name_map()
    except Exception:  # noqa: BLE001
        name_map = {}
    top = sorted(mkt, key=lambda r: -r["amount"])[:20]
    popular = []
    for r in top:
        code6 = r["code"].split(".")[0]
        popular.append({"代码": code6, "名称": name_map.get(code6, code6),
                        "最新价": round(r["close"], 2),
                        "涨跌幅": round(r["pct"], 2),
                        "成交额": r["amount"], "市盈率TTM": None,
                        "市净率": None, "ROE": None, "净利同比": None})
    results["extra"] = {"status": "ok", "data": {
        "distribution": dist,
        "summary": {"涨": sum(1 for p in pcts if p > 0),
                    "平": sum(1 for p in pcts if p == 0),
                    "跌": sum(1 for p in pcts if p < 0)},
        "popular": popular},
        "note": "分布/TOP20 由研究库前复权日线近似重建（0916 key 事故回填），估值为空"}
    print("[3/5] extra: 涨平跌", results["extra"]["data"]["summary"])


# ---------- hot_stock ----------
def do_hot_stock():
    d = ht.ht("special", "hot-stock-history", "--date", D_ISO)
    items = d.get("item") or []
    data = []
    for r in items:
        data.append({"排名": r.get("rank"), "代码": str(r.get("ticker") or ""),
                     "名称": r.get("name"), "热度": r.get("heat"),
                     "排名变动": r.get("rank_change"),
                     "趋势": r.get("rank_trend")})
    data.sort(key=lambda x: x["排名"] if x["排名"] is not None else 999)
    results["hot_stock"] = {"status": "ok", "data": data}
    print(f"[4/5] hot_stock: {len(data)} 条（字段样例 {sorted((items[0] or {}).keys()) if items else '-'}）")


# ---------- etf ----------
def do_etf():
    data = []
    for code, name in providers.ETF_LIST:
        try:
            bars = _hist_bars(("fund", "history"), code)
            bars = [b for b in bars if b[0] <= D_ISO]
            if len(bars) < 2:
                continue
            last, prev = bars[-1], bars[-2]
            data.append({"代码": code.split(".")[0], "名称": name,
                         "最新价": last[1],
                         "涨跌幅": round((last[1] / prev[1] - 1) * 100, 2),
                         "成交额(亿)": round(last[2] / 1e8, 2),
                         "换手率": None})
        except Exception as e:  # noqa: BLE001
            print("    etf 跳过", code, type(e).__name__)
    results["etf"] = {"status": "ok" if data else "error",
                      "data": data or "ETF 历史回填全部失败"}
    print(f"[4b] etf: {len(data)}/10")


# ---------- concepts（390 只，慢） ----------
def do_concepts():
    cat = providers._get_concepts()
    rows = []
    for i, (code, name) in enumerate(cat, 1):
        try:
            bars = [b for b in _hist_bars(("index", "history"), code)
                    if b[0] <= D_ISO]
            if len(bars) >= 2:
                pct = (bars[-1][1] / bars[-2][1] - 1) * 100
                rows.append({"概念": name, "涨跌幅": round(pct, 2),
                             "成交额(亿)": round(bars[-1][2] / 1e8, 2)})
        except Exception:  # noqa: BLE001
            pass
        if i % 50 == 0:
            print(f"    concepts {i}/{len(cat)}")
        time.sleep(0.25)
    rows.sort(key=lambda x: x["涨跌幅"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["涨幅排名"] = i
    results["concepts"] = {"status": "ok" if len(rows) >= 20 else "error",
                           "data": rows}
    print(f"[5/5] concepts: {len(rows)}/390")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "fast"):
        do_breadth()
        do_extra()
        do_hot_stock()
        do_etf()
    if which in ("all", "concepts"):
        do_concepts()
        # 跨进程交接：概念榜落盘（concepts 单独进程跑完立即持久化）
        fsutil.save_json_atomic(os.path.join(".status", "concepts_0915.json"),
                                results.get("concepts"))
        print("concepts 已落盘交接文件")
    if which in ("all", "merge"):
        # merge 进程重算快速模块 + 从交接文件读概念榜
        if which == "merge" and not results:
            do_breadth()
            do_extra()
            do_hot_stock()
            do_etf()
            handoff = os.path.join(".status", "concepts_0915.json")
            if os.path.exists(handoff):
                c = json.load(open(handoff, encoding="utf-8"))
                if c and c.get("status") == "ok":
                    results["concepts"] = c
        snap.setdefault("modules", {})
        for k, v in results.items():
            if v.get("status") == "ok":
                snap["modules"][k] = v
        snap["repair_note"] = ("2026-09-16 key 恢复后合并式回填：池子×3/boards/lhb（历史接口精确）"
                               "+ breadth/concepts/extra/hot_stock/etf（历史/日线近似口径，"
                               "换手率/估值/大小盘/热度明细不可回补字段如实置空）；speculation 终版池未动")
        fsutil.save_json_atomic(SNAP, snap, indent=2)
        print("合并完成 →", SNAP)
        print("模块状态:", {k: v.get("status") for k, v in snap["modules"].items()})


if __name__ == "__main__":
    main()
