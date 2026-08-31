"""项目根与默认数据目录的单一来源。

坑的来历：原先 store/ledger/signals 用**相对 cwd** 的默认路径
（"strategies_store"、"data/ledger"、"results/signals"），而 Streamlit 应用
用 `__file__` 反推的 `_ROOT` 绝对路径。两条链路各写各的目录——只要有人
从别的子目录（历史原因：原 Streamlit 界面的 `web/`，该 UI 已于 2026-08-30 移除）
启动，就会凭空生成第二个空存档库，用户感知为「存档丢了」。

规则：包内一切默认落盘路径都必须经本模块解析到项目根（quant_sim 的
父目录），或使用调用方显式传入的绝对路径；环境变量覆盖（QUANT_SIM_STORE
/ QUANT_SIM_LEDGER）保持支持，但相对值同样按项目根解析。
"""

from __future__ import annotations

import os

# quant_sim/paths.py -> quant_sim/ -> 项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve(path: str) -> str:
    """把可能相对项目根的路径变成绝对路径；已是绝对路径则原样规范化。"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(PROJECT_ROOT, path))


def default_dir(env_var: str, relative: str) -> str:
    """默认目录：环境变量优先，相对值按项目根解析。"""
    value = os.environ.get(env_var)
    return resolve(value) if value else resolve(relative)


def store_dir() -> str:
    return default_dir("QUANT_SIM_STORE", "strategies_store")


def ledger_dir() -> str:
    return default_dir("QUANT_SIM_LEDGER", os.path.join("data", "ledger"))


def signals_dir() -> str:
    return resolve(os.path.join("results", "signals"))


def results_dir() -> str:
    return resolve("results")


def data_dir() -> str:
    return resolve("data")
