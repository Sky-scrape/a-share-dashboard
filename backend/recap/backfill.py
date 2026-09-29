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

缺口判定（2026-09-30 修复）：
  - .json 与 .json.gz 归档都算「已有」——归档日（archive.py 每日把 14 天前快照
    压成 .gz 并删原文件）不算缺口，否则重抓会写出仅 6 模块的半成品 .json，
    在 snapio（.json 优先）侧遮蔽完整 .gz 归档；
  - 已归档（.json.gz 存在）的日子一律跳过，--force 也不覆盖；
  - 全部模块失败（providers 把 CLI 失败降级为 error 不抛出）的日子不落盘，
    保持缺口状态，下次运行可自动重试。
"""
import argparse
import datetime
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ht
import providers
import snapio   # .json / .json.gz 归档统一按日期键识别（快照读写单一入口）
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
        # 缺口 = 近 N 个交易日里没有快照的日期（今天由每日抓取负责）。
        # list_dates 把 .json 与 .json.gz 归档并为同一日期键：归档日不算缺口，
        # 否则会重抓归档日并写出仅 6 模块的半成品 .json，遮蔽完整 .gz 归档
        have = set(snapio.list_dates(data_dir))
        targets = [d for d in dates[:-1] if d not in have]

    if not targets:
        print("无缺口，无需补抓。")
        return

    print(f"== 补抓目标: {', '.join(targets)} ==")
    bad_days = []   # 全部模块失败未落盘的日子（保持缺口，可重试）
    for date in targets:
        out = os.path.join(data_dir, f"{date}.json")
        archived = out + ".gz"
        if os.path.exists(archived):
            # 该日已有完整快照归档（fetch_daily 全模块快照压缩而来）；backfill
            # 只能产出 6 模块半成品，写明文 .json 会在 snapio 侧遮蔽完整归档，
            # 故 --force 也不覆盖，确需重抓请先手动删除该归档文件
            print(f"  {date}: 已有归档 {date}.json.gz，跳过"
                  "（backfill 半成品会遮蔽完整归档；确需重抓请先删除该归档）")
            continue
        if os.path.exists(out) and not args.force:
            print(f"  {date}: 快照已存在，跳过（--force 可覆盖）")
            continue
        if not (len(date) == 8 and date.isdigit()):
            print(f"  {date}: 日期非法，跳过")
            continue

        providers.set_context(date, historical=True)  # 上下文唯一改写入口
        results = {name: getattr(providers, name)() for name in BACKFILL_MODULES}
        ok = sum(1 for r in results.values() if r["status"] == "ok")
        if ok == 0:
            # 全失败闸门：providers 把 CLI/上游失败降级为模块 error 不抛出，若照常
            # 落盘，缺口探测见文件存在即永久跳过，坏快照堵死自动重试路径。不落盘。
            print(f"  {date}: 0/{len(results)} 模块 ok，全部失败，不落盘"
                  "（保持缺口，下次运行自动重试）")
            for k, v in results.items():
                print(f"    {k}: {str(v.get('error', ''))[:120]}")
            bad_days.append(date)
            continue
        print(f"  {date}: {ok}/{len(results)} 模块 ok "
              + " ".join(f"{k}={len(v.get('data') or [])}" for k, v in results.items()
                         if v["status"] == "ok" and isinstance(v.get("data"), list)))

        # prev 注入：最近历史快照的涨停池（today 表现历史日期无法还原，不注入）
        prev = None
        try:
            prev_dates = sorted(d for d in snapio.list_dates(data_dir) if d < date)
            if prev_dates:
                pp = snapio.load(prev_dates[-1], data_dir) or {}
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
    if bad_days:
        print(f"== {len(bad_days)} 天全部模块失败未落盘: {', '.join(bad_days)}"
              "；请检查数据源后重跑 ==")
        sys.exit(1)


if __name__ == "__main__":
    main()
