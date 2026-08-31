# -*- coding: utf-8 -*-
"""模块契约单一来源（registry）。

providers.MODULES、check_snapshot 校验、server /api/recap/modules 都引用这里，
新增/删除模块只改本文件。

字段访问统一走 fget(row, *keys)：同一语义可能有中文/英文/带单位等多种上游 key，
读取侧容错，避免上游改名导致静默 0/None（存储仍保留中文 key，前端表格直接消费）。
"""

# 抓取顺序即此顺序；每项 (key, 中文标题, 是否允许空数据)
MODULE_REGISTRY = [
    ("market_indices",   "大盘指数",   False),
    ("breadth",          "市场情绪",   False),
    ("limit_up_pool",    "涨停池",     False),
    ("limit_down_pool",  "跌停池",     True),   # 0 只跌停是正常市场状态
    ("limit_break_pool", "炸板池",     True),   # 无炸板日正常
    ("boards",           "行业板块",   False),
    ("concepts",         "概念板块",   False),
    ("extra",            "涨跌分布/人气", False),
    ("hot_stock",        "热股榜",     True),
    ("etf",              "ETF 风向",   True),
    ("global_market",    "外围参考",   False),
    ("regulatory",       "监管榜单",   True),
    ("lhb",              "龙虎榜",     True),   # 当日可能无榜单
    ("speculation",      "投机分析",   True),   # 计算引擎在 speculate.py；旧快照可后补（回填 CLI）
]

MODULES = [k for k, _, _ in MODULE_REGISTRY]
TITLES = {k: t for k, t, _ in MODULE_REGISTRY}
ALLOW_EMPTY = {k: e for k, _, e in MODULE_REGISTRY}


def fget(row, *keys, default=None):
    """按候选 key 顺序取第一个存在且非空的值（中文/英文/变体容错）。"""
    if not isinstance(row, dict):
        return default
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "nan", "None"):
            return v
    return default


# 常用语义 → 候选 key（后端派生代码统一用这里，不再散落字符串）
CANON = {
    "code":     ("代码", "股票代码", "code", "thscode"),
    "name":     ("名称", "股票简称", "股票名称", "name"),
    "pct":      ("涨跌幅", "阶段涨跌幅", "pct"),
    "price":    ("最新价", "收盘价", "price"),
    "lb":       ("连板数", "连续涨停天数", "lb"),
    "industry": ("所属行业", "行业", "industry"),
    "amount":   ("成交额", "成交金额", "amount"),
    "seal_amt": ("封板资金", "封单额", "seal_amt"),
}


def cget(row, canon_key, default=None):
    """按 CANON 语义 key 取值。"""
    return fget(row, *CANON[canon_key], default=default)
