# -*- coding: utf-8 -*-
"""历史日K回填器（同花顺口径 · index.history 十年日线窗口）。

背景：轮动切到同花顺一级行业 90 后，盘中分钟曲线只能逐日实时累积（无分钟接口）。
但 881xxx 指数日线是同花顺真实历史：OHLC + 全天成交额俱全。本脚本把历史交易日
回填为「日K口径分时文件」：每个文件只含 **开盘 / 收盘两个真实点位**
（开盘价=09:25 集合竞价撮合值，收盘价=15:00），中间不虚构任何形状
（daily_only:true 标记，前端曲线只画点不连线，形态统计/告警不吃这些天）。

回填后各面板的诚实边界：
  - 热力图：可看当日开盘帧与收盘帧的排名/涨跌/成交额（真实）。
  - 多日轮动动画 / 轮动速度 / 强度矩阵：全可用（只用收盘值）。
  - 涨跌幅曲线：只有开盘/收盘两点，中间无采样（前端明示）。
  - 日内形态（启动/见顶/尾盘30分）：置 null，不参算。

用法:
    python backfill_ths_daily.py                       # 默认回填 2026-07-01 ~ 2026-08-31
    python backfill_ths_daily.py --from 2026-06-01 --to 2026-07-31
    python backfill_ths_daily.py --overwrite           # 覆盖已存在的日K回填文件（盘中真数据永不覆盖）
"""
import argparse
import datetime
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend", "recap"))

import ht  # noqa: E402
import ths_collect  # noqa: E402  板块池单一来源（boards.json / index catalog）

DAILY_DIR = ths_collect.DAILY_DIR
PREV_LOOKBACK_DAYS = 14   # 多拉两周历史，保证回填首日有前收盘


def bar_day(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def fetch_one(code, start_ms, end_ms):
    d = ht.ht("index", "history", "--thscode", code,
              "--start-ms", str(start_ms), "--end-ms", str(end_ms), timeout=60)
    items = d.get("item") or []
    items.sort(key=lambda x: x.get("date_ms") or 0)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-07-01")
    ap.add_argument("--to", dest="date_to", default="2026-08-31")
    ap.add_argument("--overwrite", action="store_true",
                    help="重写已存在的日K回填文件（盘中逐轮采集的真数据永远跳过）")
    args = ap.parse_args()
    d_from = datetime.date.fromisoformat(args.date_from)
    d_to = datetime.date.fromisoformat(args.date_to)
    if d_to > datetime.date.today():
        d_to = datetime.date.today()
    start = d_from - datetime.timedelta(days=PREV_LOOKBACK_DAYS)
    start_ms = int(time.mktime(start.timetuple()) * 1000)
    end_ms = int(time.mktime(d_to.timetuple()) * 1000) + 86400 * 1000

    pool = ths_collect.load_board_pool()
    names = dict(pool)
    print(f"回填 {args.date_from} ~ {args.date_to} · 同花顺一级行业 {len(pool)} 个（日线窗口含前收）")

    # code -> {date: bar}
    hist = {}
    fails = []
    for i, (code, name) in enumerate(pool):
        try:
            hist[code] = {bar_day(b["date_ms"]): b for b in fetch_one(code, start_ms, end_ms)
                          if b.get("date_ms") and b.get("close_price")}
        except Exception as e:  # noqa: BLE001 - 单行业失败不影响整体
            hist[code] = {}
            fails.append((code, name, str(e)[:100]))
        if (i + 1) % 15 == 0 or i + 1 == len(pool):
            print(f"  历史拉取 {i + 1}/{len(pool)}", flush=True)
        time.sleep(0.2)   # 温和限速
    ok_codes = [c for c, _ in pool if hist.get(c)]
    if len(ok_codes) < len(pool) * 0.8:
        print(f"!! 成功拉取仅 {len(ok_codes)}/{len(pool)}，中止（不写半成品回填）", file=sys.stderr)
        for f_ in fails[:10]:
            print("   ", f_, file=sys.stderr)
        sys.exit(1)
    for f_ in fails:
        print(f"  [warn] 拉取失败跳过: {f_[0]} {f_[1]} {f_[2]}")

    # 交易日 = 全部成功行业都有该日 bar（保证矩阵/动画板块齐）；首日需有前一 bar 作前收
    all_dates = set.intersection(*[set(hist[c]) for c in ok_codes])
    prev_of = {}
    for code in ok_codes:
        ds = sorted(hist[code])
        idx = {d: k for k, d in enumerate(ds)}
        for d in ds:
            k = idx[d]
            prev_of[(code, d)] = hist[code][ds[k - 1]]["close_price"] if k > 0 else None
    days = sorted(d for d in all_dates if d >= args.date_from and d <= args.date_to
                  and all(prev_of.get((c, d)) for c in ok_codes))
    print(f"可回填交易日 {len(days)} 天：{days[0] if days else '-'} ~ {days[-1] if days else '-'}")

    written, skipped_live = 0, 0
    for day in days:
        path = os.path.join(DAILY_DIR, f"{day}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                if not old.get("daily_only") and not args.overwrite:
                    skipped_live += 1
                    continue
            except Exception:
                pass
        boards, series = [], {}
        for code in ok_codes:
            b = hist[code][day]
            prev = prev_of[(code, day)]
            o_pct = round((b["open_price"] / prev - 1) * 100, 2)
            c_pct = round((b["close_price"] / prev - 1) * 100, 2)
            boards.append({"code": code, "name": names.get(code, code),
                           "type": "industry", "mcap": 0})
            series[code] = {"name": names.get(code, code), "type": "industry",
                            "times": ["开盘", "收盘"],
                            "pcts": [o_pct, c_pct],
                            "prices": [round(b["open_price"], 2), round(b["close_price"], 2)],
                            # 开盘时刻的成交额日线没有 → 0（前端按最小面积处理）；收盘=全天真实成交额
                            "amounts": [0, float(b.get("turnover") or 0)]}
        out = {"date": day, "src": "ths-daily", "daily_only": True,
               "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "backfilled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "boards": boards, "series": series, "failed": [],
               "times": ["开盘", "收盘"],
               "coverage": {"minutes": 2, "last_time": "收盘", "complete": False,
                            "source": "同花顺指数日线回填（index.history，仅开盘/收盘两点）",
                            "note": "历史日K回填：非盘中逐分钟数据，中间走势未采样，不虚构"}}
        os.makedirs(DAILY_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, path)
        written += 1
        if written % 10 == 0:
            print(f"  写入 {written}/{len(days)} …", flush=True)
    print(f"完成：回填 {written} 天（跳过盘中真实日 {skipped_live}）-> {DAILY_DIR}")


if __name__ == "__main__":
    main()
