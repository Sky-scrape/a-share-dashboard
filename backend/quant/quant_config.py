# -*- coding: utf-8 -*-
"""量化平台板块配置：内置引擎根 + 任务目录（单一来源）。

量化平台（原独立项目）已于 2026-08-30 整体迁入本项目的 quant/ 目录并移除 Streamlit UI 只留纯引擎；
引擎、数据缓存、策略存档、结果产物都在 A/quant/ 下；本板块通过
quant_api（快查询进程内直跑）与 run_job（长任务子进程）驱动引擎。
"""
import os

_A_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 内置量化引擎根（纯引擎，无 UI）；可用 A_QUANT_ROOT 覆盖
QUANT_ROOT = os.environ.get("A_QUANT_ROOT", os.path.join(_A_ROOT, "quant"))

# 长任务（选股/信号/网格）的临时产物目录：留在 A 侧，不污染引擎目录
JOBS_DIR = os.path.join(_A_ROOT, "data", "quant", "jobs")
JOBS_KEEP = 30          # 只保留最近 N 个任务文件

# 采集上限（防止流水文件无限增长拖慢页面）
MAX_BACKTEST_LOG = 60      # 回测流水最多取最近 N 条
MAX_ITEMS_PER_DIR = 40     # 目录清单最多取最近 N 项
