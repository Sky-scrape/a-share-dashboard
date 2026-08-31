"""策略存档「我的策略」：表单规则 / 在线代码 / 内置参数统一存 JSON，可回放可分享。

存储布局：项目根 `strategies_store/<安全名>.json`，每文件一条：
    {"name", "kind": rule|code|builtin, "created", "updated", "note",
     "payload": {spec / {code} / {family, params}},
     "meta": {"data_symbols": [...]}}

kind=rule   payload=GenericRuleStrategy 的 spec（纯 JSON）
kind=code   payload={"code": "..."}（沙箱编译）
kind=builtin payload={"family": "dual_ma|momentum|meanrev", "params": {...}}
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .. import paths as _paths

_BUILTIN_FAMILIES = {
    "dual_ma": "quant_sim.strategies:DualMAStrategy",
    "momentum": "quant_sim.strategies:MomentumRankingStrategy",
    "meanrev": "quant_sim.strategies:MeanReversionStrategy",
}


def _safe(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name.strip())
    return s or "strategy"


@dataclass
class SavedStrategy:
    name: str
    kind: str
    payload: dict
    note: str = ""
    data_symbols: List[str] = field(default_factory=list)
    updated: str = ""
    created: str = ""
    version: int = 1

    @property
    def filename(self) -> str:
        return _safe(self.name) + ".json"


def _dir(store_dir: Optional[str]) -> str:
    # 不依赖 cwd：显式传入的相对目录与环境变量覆盖都按项目根解析。
    if store_dir:
        return _paths.resolve(store_dir)
    return _paths.store_dir()


def save_strategy(
    name: str,
    kind: str,
    payload: dict,
    note: str = "",
    data_symbols: Optional[List[str]] = None,
    store_dir: Optional[str] = None,
    overwrite: bool = True,
) -> str:
    if kind not in ("rule", "code", "builtin"):
        raise ValueError(f"未知策略类型 {kind}")
    if kind == "rule":
        try:
            json.dumps(payload)  # spec 必须纯 JSON
        except TypeError as e:
            raise ValueError(f"规则 spec 不是合法 JSON：{e}") from None
    d = _dir(store_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _safe(name) + ".json")
    created, version = time.strftime("%Y-%m-%d %H:%M"), 1
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"策略「{name}」已存在（换名或允许覆盖）")
        try:  # 覆盖保存：保留首次创建时间，版本号自增（研究迭代可追溯）
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            created = old.get("created") or old.get("updated") or created
            version = int(old.get("version", 1)) + 1
        except Exception:
            pass
    doc = {
        "name": name.strip(), "kind": kind, "payload": payload, "note": note,
        "data_symbols": list(data_symbols or []),
        "created": created, "version": version,
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


def list_strategies(store_dir: Optional[str] = None) -> List[SavedStrategy]:
    d = _dir(store_dir)
    out: List[SavedStrategy] = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                doc = json.load(f)
            out.append(SavedStrategy(
                name=doc.get("name", fn[:-5]), kind=doc.get("kind", "rule"),
                payload=doc.get("payload", {}), note=doc.get("note", ""),
                data_symbols=doc.get("data_symbols", []), updated=doc.get("updated", ""),
                created=doc.get("created", ""), version=int(doc.get("version", 1) or 1),
            ))
        except Exception:
            continue  # 坏文件不阻塞列表
    return out


def load_strategy(name: str, store_dir: Optional[str] = None) -> SavedStrategy:
    for s in list_strategies(store_dir):
        if s.name == name or s.filename == _safe(name) + ".json":
            return s
    raise FileNotFoundError(f"没有找到存档策略「{name}」，现有：{[s.name for s in list_strategies(store_dir)]}")


def delete_strategy(name: str, store_dir: Optional[str] = None) -> bool:
    try:
        s = load_strategy(name, store_dir)
        os.remove(os.path.join(_dir(store_dir), s.filename))
        return True
    except FileNotFoundError:
        return False


def build_saved(s: SavedStrategy, universe: Optional[List[str]] = None, *, allow_exec: bool = False):
    """存档 → Strategy 实例。universe 用于给内置/规则策略补默认标的池。

    ⚠️ kind=code 存档回放会**重新 exec 存档里的源码**（分享 JSON = 分享可执行代码，
    沙箱防误不防恶）。因此默认拒绝执行，调用方必须确认来源可信后传 allow_exec=True：
    UI 用策略库页的显式勾选，CLI 用 --yes-run-code。
    """
    if s.kind == "rule":
        from .rule_based import GenericRuleStrategy

        spec = dict(s.payload)
        spec.setdefault("symbols", universe or [])
        return GenericRuleStrategy(**spec)
    if s.kind == "code":
        if not allow_exec:
            raise PermissionError(
                f"策略「{s.name}」是代码存档，回放需重新执行其源码。"
                "沙箱防误不防恶：只有确认该 JSON 来源可信才继续——"
                "网页请在策略库页勾选「允许执行代码存档」，CLI 请加 --yes-run-code。"
            )
        from .sandbox import build_user_strategy

        return build_user_strategy(s.payload["code"])
    if s.kind == "builtin":
        fam = s.payload.get("family")
        ref = _BUILTIN_FAMILIES.get(fam)
        if ref is None:
            raise ValueError(f"未知内置策略族 {fam}")
        mod, cls = ref.split(":")
        import importlib

        klass = getattr(importlib.import_module(mod), cls)
        return klass(**s.payload.get("params", {}))
    raise ValueError(f"未知类型 {s.kind}")


def strategy_description(s: SavedStrategy) -> str:
    """一行人话描述，供列表展示。"""
    if s.kind == "rule":
        from .rule_based import _describe_rule

        p = s.payload
        try:
            ent = _describe_rule(p.get("entry")) if (p.get("entry") or {}).get("conds") else "无条件"
            exi = _describe_rule(p.get("exit")) if (p.get("exit") or {}).get("conds") else "仅风控"
            return f"{'×'.join(p.get('symbols', []))}｜入场 {ent}｜出场 {exi}｜仓位 {p.get('target_weight', 0.95):.0%}"
        except Exception:
            return "规则策略"
    if s.kind == "code":
        first = next((ln.strip() for ln in s.payload.get("code", "").splitlines() if ln.strip() and not ln.startswith("#")), "代码策略")
        return first[:60]
    return f"内置 {s.payload.get('family')} {json.dumps(s.payload.get('params', {}), ensure_ascii=False)[:50]}"
