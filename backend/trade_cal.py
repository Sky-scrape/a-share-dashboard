# -*- coding: utf-8 -*-
"""交易日历判定 · 单一来源（2026-09-04 收敛）。

is_trade_today() 曾在 rotation/ths_collect.py 与 auction/auc_collector.py 各有一份
逐行相同的拷贝，而竞价侧早期还因日历格式不一致（"%Y-%m-%d" vs 8 位 "20260901"）
把每个交易日判成非交易日、采集静默停摆一个月（README 2026-08-31 事故）。现在只建一处：

- 两侧日期都只取数字比较，上游格式再变（带横杠/带时间）也不受影响；
- 拿不到可用日期（schema 变了）返回 None 让调用方按交易日继续，而不是 False——
  误判「非交易日」是静默失效，比多跑一次严重得多；
- cal 参数可注入：调用方（如 auc_collector）把「ht.ht("market","calendar")」以
  lambda 传入，既便于 smoke 换桩测试，又不引入第二个取数口径。
"""
import re
import sys
import time


def _default_log(msg):
    print(msg, file=sys.stderr)


def is_trade_today(today8=None, cal=None, log=None):
    """hithink 交易日历探测当日是否交易；无法验证返回 None（调用方按交易日继续）。

    cal: 无参函数，返回 market.calendar 的 data 部分；缺省用 ht.ht 现抓。
    """
    if cal is None:
        import ht  # noqa: PLC0415  延迟导入：宿主进程 import 本模块不强制拉起 CLI
        cal = lambda: ht.ht("market", "calendar")  # noqa: E731
    log = log or _default_log
    try:
        item = (cal() or {}).get("item") or []
        today = today8 or time.strftime("%Y%m%d")
        dates = {re.sub(r"\D", "", str(x.get("date") or "")) for x in item}
        dates = {d for d in dates if len(d) == 8}
        if not dates:
            log("交易日历无可用日期（字段/格式变了？），按交易日继续")
            return None
        return today in dates
    except Exception as e:  # noqa: BLE001
        log(f"交易日历不可用（{type(e).__name__}），按交易日继续")
        return None
