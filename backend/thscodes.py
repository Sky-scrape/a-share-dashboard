# -*- coding: utf-8 -*-
"""6 位代码 → thscode 转换 · 单一来源（2026-09-04 收敛）。

此前三份实现且规则不一致：
    auction/auc_config.to_thscode   最全（本文件的母本）
    recap/speculate.thscode_of      少 B股/基金分支（碰巧池子里没出过事）
    global/fetch_global._thscode    会把 5/9 开头（沪基金/沪B）误归北交所
上游目录或代码段变化时只改这里；auc_config 保留同名转发兼容旧调用。
"""
import re

_CODE_RE = re.compile(r"^\d{6}$")


def to_thscode(code):
    """6 位代码 → 完整 thscode；无法识别返回 None。

    入参容忍 sh/sz/bj 前缀与「600519.SH」写法。覆盖北交所（4xxxxx/8xxxxx/920xxx 新段）
    与 B 股（900 沪B、200 深B）、基金（5xx 沪、15/16/18 深），否则这些标的会被
    归为「认不出」静默掉出观察池。"""
    c = str(code or "").strip().lower()
    c = re.sub(r"^(sh|sz|bj)[.]?", "", c)
    c = c.split(".")[0]
    if not _CODE_RE.fullmatch(c):
        return None
    if c.startswith("92") or c.startswith(("4", "8")):
        return c + ".BJ"                      # 北交所（920 新段要排在 9 前，避免被当成沪 B）
    if c.startswith(("6", "5", "9")):
        return c + ".SH"                      # 沪市：60/68 A、50x/51x/52x/56x/58x 基金、900 B股
    if c.startswith(("0", "3", "1", "2")):
        return c + ".SZ"                      # 深市：00/30 A、15x/16x/18x 基金、200 B股
    return None
