"""参数邻域敏感性：把策略的数值参数各 ±pct 扰动一批变体，回答
"我选的不是过拟合尖峰，而是稳健成片"。孤立最优=假象，邻域成片才是信号。

perturb_variants 递归找 dict/list 里的整数参数（跳过 bool 与 <3 的值），
每个生成 ×(1-pct)、×(1+pct) 两个变体，返回 (人类标签, 深拷贝后新对象) 列表。
"""

from __future__ import annotations

import copy
from typing import Any, List, Tuple


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _walk(obj: Any, path: Tuple[Any, ...], out: List[Tuple[Tuple[Any, ...], int]]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_int(v):
                out.append((path + (k,), v))
            else:
                _walk(v, path + (k,), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, path + (i,), out)


def _set_at(obj: Any, path: Tuple[Any, ...], value: int) -> None:
    cur = obj
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value


def _label(path: Tuple[Any, ...], old: int, new: int) -> str:
    key = path[-1] if isinstance(path[-1], str) else f"[{path[-1]}]"
    # 带上下文段（entry/exit 等顶层键）区分同名参数出现在多处的情形
    ctx = next((p for p in path if isinstance(p, str) and p in ("entry", "exit", "stop", "risk")), "")
    return f"{ctx + '·' if ctx else ''}{key} {old}→{new}"


def perturb_variants(
    params: Any,
    pct: float = 0.20,
    skip_keys: Tuple[str, ...] = ("top_n", "universe"),
    max_variants: int = 12,
) -> List[Tuple[str, Any]]:
    """返回 [(标签, 扰动后的深拷贝 params)]。同一 key 的 ±两向若撞值则去重。

    skip_keys 默认跳 top_n（持仓数，±20% 常落回原值或改变资金结构而非平滑敏感性）。
    """
    sites: List[Tuple[Tuple[Any, ...], int]] = []
    _walk(params, (), sites)
    out: List[Tuple[str, Any]] = []
    seen = set()
    for path, val in sites:
        key = path[-1]
        if isinstance(key, str) and key in skip_keys:
            continue
        if val < 3:  # 太小，扰动只是整数跳变
            continue
        for mult in (1 - pct, 1 + pct):
            new = int(round(val * mult))
            if new == val or new < 2:
                continue
            sig = (path, new)
            if sig in seen:
                continue
            seen.add(sig)
            clone = copy.deepcopy(params)
            _set_at(clone, path, new)
            out.append((_label(path, val, new), clone))
            if len(out) >= max_variants:
                return out
    return out
