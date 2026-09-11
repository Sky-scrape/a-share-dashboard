# -*- coding: utf-8 -*-
"""历史补抓：用 hithink-finance 的历史接口回补断档交易日。

背景：2026-08-22 起 akshare 因 Python 升级丢失，定时任务连续失败，
8-24 ~ 8-27 快照缺失。hithink 池子/龙虎榜/行业日线支持历史日期，
可回补核心复盘模块；当日快照类模块（指数/概念/热股/ETF/外围）无法回补。

用法：
    python backend/backfill.py                    # 补抓 data/ 缺失的交易日
    python backend/backfill.py --date 20260825    # 补抓指定日期
    python backend/backfill.py --force            # 已有快照也重抓（覆盖）

补抓模块：limit_up_pool / limit_down_pool / limit_break_pool / boards / lhb / regulatory
输出：data/<date>.json（modules 仅含上述模块；check_snapshot 不适用）
"""
import argparse
import datetime
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ht
import providers
import fsutil

BACKFILL_MODULES = [
    "limit_up_pool",
    "limit_down_pool",
    "limit_break_pool",
    "boards",
    "lhb",
    "regulatory",
]

# 项目根/data/recap（合并后统一数据目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "recap")


def recent_trade_dates(keep=120):
    """从 hithink 日历取近 N 个已过去的交易日（含今天）。"""
    cal = ht.ht("market", "calendar")
    today = datetime.date.today().strftime("%Y%m%d")
    dates = sorted(str(x.get("date")) for x in (cal.get("item") or [])
                   if str(x.get("date", "")) <= today)
    return dates[-keep:]


def main():
    ap = argparse.ArgumentParser(description="历史补抓（hithink 历史接口）")
    ap.add_argument("--date", default=None, help="8 位日期；缺省自动找 data/ 缺口")
    ap.add_argument("--force", action="store_true", help="已有快照也重抓")
    args = ap.parse_args()

    data_dir = DATA_DIR
    os.makedirs(data_dir, exist_ok=True)

    if args.date:
        targets = [args.date]
    else:
        dates = recent_trade_dates()
        # 缺口 = 近 N 个交易日里没有快照文件的日期（今天由每日抓取负责）
        targets = [d for d in dates[:-1]
                   if not os.path.exists(os.path.join(data_dir, f"{d}.json"))]

    if not targets:
        print("无缺口，无需补抓。")
        return

    print(f"== 补抓目标: {', '.join(targets)} ==")
    for date in targets:
        out = os.path.join(data_dir, f"{date}.json")
        if os.path.exists(out) and not args.force:
            print(f"  {date}: 快照已存在，跳过（--force 可覆盖）")
            continue
        if not (len(date) == 8 and date.isdigit()):
            print(f"  {date}: 日期非法，跳过")
            continue

        providers.set_context(date, historical=True)  # 上下文唯一改写入口
        results = {name: getattr(providers, name)() for name in BACKFILL_MODULES}
        ok = sum(1 for r in results.values() if r["status"] == "ok")
        print(f"  {date}: {ok}/{len(results)} 模块 ok "
              + " ".join(f"{k}={len(v.get('data') or [])}" for k, v in results.items()
                         if v["status"] == "ok" and isinstance(v.get("data"), list)))

        # prev 注入：最近历史快照的涨停池（today 表现历史日期无法还原，不注入）
        prev = None
        try:
            prev_dates = sorted(
                (f[:-5] for f in os.listdir(data_dir)
                 if len(f) == 13 and f.endswith(".json") and f[:-5] < date))
            if prev_dates:
                with open(os.path.join(data_dir, prev_dates[-1] + ".json"),
                          encoding="utf-8") as pf:
                    pp = json.load(pf)
                rows = (pp.get("modules", {}).get("limit_up_pool", {}) or {}).get("data") or []
                if rows:
                    prev = {
                        "date": prev_dates[-1],
                        "zt_codes": [str(r["代码"]) for r in rows if r.get("代码") is not None],
                        "zt_count": len(rows),
                        "max_lb": max((int(r.get("连板数") or 0) for r in rows), default=0),
                        "today": None,
                    }
        except Exception as e:  # noqa: BLE001
            print(f"    prev 注入失败（忽略）: {type(e).__name__}")

        snapshot = {
            "date": date,
            "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "modules": results,
            "prev": prev,
            "backfill": True,
        }
        fsutil.save_json_atomic(out, snapshot, indent=2)   # 原子写（backend/fsutil.py）
        print(f"    已写入 {out}")

    providers.set_context(providers.DATE, historical=False)  # 复位历史模式


if __name__ == "__main__":
    main()
