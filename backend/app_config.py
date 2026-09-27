# -*- coding: utf-8 -*-
"""运营常量薄层外部化（方案 S-5，2026-09-27）。

原则（docs/产品优化方案-20260927.md S-5）：只收**运营偏好**——保留天数、
留存上限这类「改起来不该动代码」的数值；口径类常量一律不外部化：执行层
（backend/execution_layer.py）是回测口径，竞价时间线（auc_config）与计划任务
时刻强耦合，改它们必须走代码 + 回归测试。

用法：`.status/config.json`（可选，gitignored）写同名键覆盖默认值，例如：
    {"recap_keep_days": 200, "logs_keep_days": 14}
缺文件 / 坏 JSON / 未知键 → 全部回落 DEFAULTS，行为与外部化之前完全一致。
配置在进程启动后首次取值时读一次并缓存——**不热生效**，改完重启对应链路，
与改代码同一心智。notify/backup/watchdog 各自的 .status/*.json 不并入这里。

当前键集（即 DEFAULTS 的键）：recap_keep_days / rotation_keep_days /
logs_keep_days / quant_jobs_keep。
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(_HERE), ".status", "config.json")

# 代码默认值 = 历史行为；.status/config.json 同名键覆盖
DEFAULTS = {
    "recap_keep_days": 400,     # 复盘快照保留天数（fetch_daily --keep-days 缺省）
    "rotation_keep_days": 120,  # 轮动分时保留天数（ths_collect --keep-days 缺省）
    "logs_keep_days": 30,       # .status/logs 杂项日志保留天数（watchdog.prune_logs）
    "quant_jobs_keep": 30,      # 量化长任务文件留存份数（quant_config.JOBS_KEEP）
}

_cache = None


def _load():
    global _cache
    if _cache is None:
        cfg = {}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg = {k: v for k, v in raw.items() if k in DEFAULTS}
        except (OSError, ValueError):
            pass   # 缺文件/坏 JSON：整份回落默认（与「未外部化」等价）
        merged = {**DEFAULTS, **cfg}
        # 数值归一（复审 P2）：配置写错类型（如 "30"）不能在消费端炸——
        # watchdog 健康主链路每次运行都会取 logs_keep_days
        for k in merged:
            try:
                merged[k] = int(merged[k])
            except (TypeError, ValueError):
                merged[k] = DEFAULTS[k]
        _cache = merged
    return _cache


def get(key):
    """取运营常量。未知键抛 KeyError——拼写错误应当炸，不该静默回默认。"""
    return _load()[key]
