# -*- coding: utf-8 -*-
"""交易日历判定 · 单一来源（2026-09-04 收敛）。

is_trade_today() 曾在 rotation/ths_collect.py 与 auction/auc_collector.py 各有一份
逐行相同的拷贝，而竞价侧早期还因日历格式不一致（"%Y-%m-%d" vs 8 位 "20260901"）
把每个交易日判成非交易日、采集静默停摆一个月（README 2026-08-31 事故）。现在只建一处：

- 两侧日期都只取数字比较，上游格式再变（带横杠/带时间）也不受影响；
- cal 注入时（单测/smoke 换桩）即为单一来源：日期成员判定即结论，拿不到结论
  （报错/字段变了）返回 None 让调用方按交易日继续，绝不本地兜底、绝不返回
  False 静默跳过；
- 默认路径（真实 hithink）拿不到时退回本地日历（data/recap/us_market/ashare_cal.json，
  akshare 新浪口径全量、us_market 每日刷新）：2026-10-02/10-05 假期 hithink 断源
  返回 None，轮动采集器按交易日跑了整天、把冻结在 9-30 收盘的快照当真实分时
  写盘并刷进面板（server 按最大日期还把 10-05 当最新交易日）——本地兜底后，
  日历覆盖今日而今日不在其中，即权威判非交易日；
- 默认路径仅在日历覆盖今日（min≤today≤max）时下结论：日历陈旧没覆盖到今天时
  「不在其中」不能当非交易日，方向反了就是 08-31 静默停摆同款事故；
- 两级都拿不到结论才返回 None（误判「非交易日」是静默失效，比多跑一次严重得多）；
- cal 参数可注入：调用方（如 auc_collector）把「ht.ht("market","calendar")」以
  lambda 传入，既便于 smoke 换桩测试，又不引入第二个取数口径。
"""
import json
import os
import re
import sys
import time


def _default_log(msg):
    print(msg, file=sys.stderr)


# 本地日历单一来源：us_market 的 A 股日历缓存（akshare 新浪口径全量，7 天 TTL 刷新）
_LOCAL_CAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "recap", "us_market", "ashare_cal.json")


def _local_dates8():
    """本地日历缓存里的 8 位交易日集合；文件缺失/损坏/为空返回 None。"""
    try:
        with open(_LOCAL_CAL, encoding="utf-8") as f:
            dates = json.load(f).get("dates") or []
    except Exception:  # noqa: BLE001  缓存缺失/损坏都算「无本地日历」
        return None
    d8 = {re.sub(r"\D", "", str(x)) for x in dates}
    d8 = {d for d in d8 if len(d) == 8}
    return d8 or None


def is_trade_today(today8=None, cal=None, log=None):
    """hithink 交易日历探测当日是否交易；默认路径 hithink 不可用退回本地日历，
    两级都拿不到结论返回 None（调用方按交易日继续）。

    cal: 无参函数，返回 market.calendar 的 data 部分；缺省用 ht.ht 现抓。
    注入时即为单一来源（成员判定即结论，拿不到结论返回 None，不越源兜底）。
    """
    injected = cal is not None
    if not injected:
        import ht  # noqa: PLC0415  延迟导入：宿主进程 import 本模块不强制拉起 CLI
        cal = lambda: ht.ht("market", "calendar")  # noqa: E731
    log = log or _default_log
    today = today8 or time.strftime("%Y%m%d")
    try:
        item = (cal() or {}).get("item") or []
        dates = {re.sub(r"\D", "", str(x.get("date") or "")) for x in item}
        dates = {d for d in dates if len(d) == 8}
        if dates:
            if injected or min(dates) <= today <= max(dates):
                return today in dates
            log(f"交易日历未覆盖今日（范围 {min(dates)}~{max(dates)}），退回本地日历")
        else:
            log("交易日历无可用日期（字段/格式变了？）"
                + ("" if injected else "，退回本地日历"))
    except Exception as e:  # noqa: BLE001
        log(f"交易日历不可用（{type(e).__name__}）"
            + ("" if injected else "，退回本地日历"))
    if injected:
        return None
    local = _local_dates8()
    if local is None:
        log("本地日历不可用（缺 data/recap/us_market/ashare_cal.json），按交易日继续")
        return None
    if min(local) <= today <= max(local):
        if today in local:
            return True
        log("本地日历覆盖今日且今日不在其中：非交易日")
        return False
    log("本地日历未覆盖今日，按交易日继续")
    return None
