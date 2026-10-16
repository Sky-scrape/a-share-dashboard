# -*- coding: utf-8 -*-
"""一次性回填：修复 providers.boards 的 .TI 双后缀 bug 造成的行业 5 日线丢失。

背景（2026-09-20 发现并修复）：
  providers._get_industries() 返回的 thscode 已含 ".TI"（形如 "881121.TI"），
  而 boards() 又拼了一次 ".TI" 得到 "881121.TI.TI"，被上游参数校验拒绝
  （CLI_BAD_ARGUMENT: must match /^\\d{6}\\.(SH|SZ|BJ|TI)$/u），异常被
  `except Exception: return code, []` 吞掉后降级为空历史——快照里
  boards.history 只剩「当天兜底补的一行」，前端点行业看近 5 日走势画不出折线。

  影响范围（实扫全 174 份快照确认）：20260910 ~ 20260918 共 7 个交易日，
  每天 90 个行业的 history 深度均为 1（应为 5）。更早的 20260224/25/27 与
  20260817/19 也有不足 5 条的情况，但成因不同（历史回补期的口径差异），
  不在本次范围内。

  根因修复已落在 backend/recap/providers.py（改为直接传 code，不再拼 .TI）。
  本脚本只回填已经落盘的、受该 bug 影响的历史快照。

严格不做的事：
  - 只改 modules.boards.data 里的 history 字段与涨跌幅（用同口径重取）；
  - 不动其他模块、不动 fetched_at（其余数据仍是当日抓取的原值）；
  - 不动任何 pool / speculation 数据。

用法：
    python .status/repair_boards_history.py --dry-run
    python .status/repair_boards_history.py
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))

import fsutil     # noqa: E402  原子写盘单一来源
import providers  # noqa: E402  行业板块数据源（已修复 .TI 拼接口径）
import snapio     # noqa: E402  快照读写单一来源

# 受 .TI 双后缀 bug 影响的交易日（实扫确认，history 深度恒为 1）
TARGETS = ["20260910", "20260911", "20260914", "20260915",
           "20260916", "20260917", "20260918",
           # 2026-09-21 扩充：更早 5 天同属 boards 数据损坏，但成因与 .TI bug 不同
           # （见各日注释），一并按同一口径重取。
           "20260224", "20260225", "20260227", "20260817", "20260819"]

# 这 5 天的具体损坏形态（实扫记录，供人工复核时对照）：
#   20260224/20260225  history 含超出快照日的「未来日」行（02-25/02-26 出现在
#                      02-24/02-25 的快照里），且同一天重复一行、价格与权威源不符；
#                      字段涨跌幅亦错（保险 记 +2.56%，权威源实为 −1.46%）
#   20260227           深度 4（缺一行）
#   20260817           89 个行业 history 仅 1 行且收盘价为 null
#   20260819           90 条里仅 51 个唯一行业名（39 个重复），且全部涨跌幅=0.0
#                      —— renderOverview 取 boards[0] 当「主线」，会显示错误主线
_BACKFILL_DAYS = {"20260224", "20260225", "20260227", "20260817", "20260819"}


def _health(rows):
    """数据健康度体检（用于回填前后对比与「是否真的变好」判定）。

    注：零涨跌幅本身不是损坏信号——个股/行业停牌或微幅波动时 0.00% 完全正常
    （0917 实例：煤炭开采加工 3018.48->3018.51 四舍五入后即 0.0）。只有
    「绝大多数行业都恰为 0.0」（0819 实例 90/90）才属异常，故用比例判据。"""
    names = [r.get("名称") for r in rows]
    n = len(rows) or 1
    n_none = sum(1 for r in rows for h in (r.get("history") or [])
                 if h.get("收盘价") is None)
    n_zero = sum(1 for r in rows if r.get("涨跌幅") == 0.0)
    return {"n": len(rows), "uniq": len(set(names)),
            "dup": len(names) - len(set(names)),
            "min_dep": min((len(r.get("history") or []) for r in rows), default=0),
            "none_close": n_none, "zero_pct": n_zero,
            "zero_ratio": round(n_zero / n, 3)}


def main():
    ap = argparse.ArgumentParser(description="回填受 .TI bug 影响的行业 5 日线")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不写盘")
    args = ap.parse_args()

    n_done = 0
    for date8 in TARGETS:
        snap = snapio.load(date8)
        if snap is None:
            print(f"  {date8}: 快照缺失，跳过")
            continue
        old = ((snap.get("modules") or {}).get("boards") or {}).get("data") or []
        oh = _health(old)

        providers.set_context(date8, historical=True)
        try:
            m = providers.boards()
        except Exception as e:  # noqa: BLE001
            print(f"  {date8}: 重取失败 {type(e).__name__}: {str(e)[:120]}")
            continue
        if m.get("status") != "ok" or not m.get("data"):
            print(f"  {date8}: 重取返回 {m.get('status')} / 空数据，跳过")
            continue
        new = m["data"]
        nh = _health(new)
        print(f"  {date8}: 深度 {oh['min_dep']}->{nh['min_dep']} · 唯一名 {oh['uniq']}->{nh['uniq']} "
              f"· 重复 {oh['dup']}->{nh['dup']} · 空收盘 {oh['none_close']}->{nh['none_close']} "
              f"· 涨跌幅0 {oh['zero_pct']}->{nh['zero_pct']}")
        # 改善判据：无重复名 + 深度满 5 + 无空收盘 + 零涨跌幅不超过 20%（比例判据，
        # 容忍个别行业真为 0.00%；0819 的 90/90 异常会被拦下）。任一未达则不写。
        if nh["dup"] or nh["min_dep"] < 5 or nh["none_close"] or nh["zero_ratio"] > 0.2:
            print(f"    {date8}: 重取结果未达健康阈值，跳过（不拿未达标数据覆盖）")
            continue
        if oh["dup"] == nh["dup"] and oh["min_dep"] == nh["min_dep"] \
                and oh["none_close"] == nh["none_close"] and oh["zero_pct"] == nh["zero_pct"]:
            print(f"    {date8}: 无改善，跳过")
            continue
        if args.dry_run:
            print("    [dry-run] 未写盘")
            continue
        snap = json.loads(json.dumps(snap))   # 脱手 snapio 共享缓存
        snap["modules"]["boards"] = m
        # 保持原格式写回（这些早期快照是 .json.gz，不能另生成一份 .json）
        p = snapio.snap_path(snapio.RECAP_DATA, date8)
        if p.endswith(".gz"):
            fsutil.save_gzip_json_atomic(p, snap)
        else:
            snapio.save(date8, snap)
        n_done += 1
        print(f"    已写回 {os.path.basename(p)}")
    print(f"== 完成：写回 {n_done} 份" + ("（dry-run）" if args.dry_run else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
