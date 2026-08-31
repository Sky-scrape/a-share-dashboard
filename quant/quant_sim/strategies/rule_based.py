"""声明式规则策略：把「指标 + 条件 + 入场/出场 + 止损止盈」表达为可序列化的 dict。

设计目标：让 Streamlit 表单（无代码）拼出规则 JSON，交给 GenericRuleStrategy 执行；
同时 generate_python_code() 能导出等价的裸 Python 策略，方便用户日后转成代码策略。

规则 JSON 约定（全部可 json.dumps，便于保存/分享策略配置）：

    operand = {"t": "field", "f": "close"}                      # 原始价量字段
            | {"t": "ind", "i": "sma", "p": {"n": 20}, "c": ""} # 指标（可含组件 c）
            | {"t": "const", "v": 0.5}                          # 常数
    cond    = {"left": operand, "op": "gt|lt|ge|le|cross_above|cross_below", "right": operand}
    rule    = {"mode": "all|any", "conds": [cond, ...]}

支持的指标：sma/ema/rsi/boll(upper,mid,lower)/donchian(upper,mid,lower)/
macd(dif,dea,hist)/mom/atr/vol_ratio。指标一律基于 ctx.history（默认截至昨收），
天然规避未来函数；当日信号 → T+1 开盘成交由引擎完成。
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.strategy_base import Strategy

FIELDS = ["close", "open", "high", "low", "pre_close", "volume", "amount"]
INDICATORS: Dict[str, dict] = {
    "sma": {"label": "均线 SMA", "params": {"n": 20}, "comps": [""]},
    "ema": {"label": "指数均线 EMA", "params": {"n": 20}, "comps": [""]},
    "rsi": {"label": "RSI 相对强弱", "params": {"n": 14}, "comps": [""]},
    "boll": {"label": "布林带 BOLL", "params": {"n": 20, "k": 2.0}, "comps": ["upper", "mid", "lower"]},
    "donchian": {"label": "唐奇安通道", "params": {"n": 20}, "comps": ["upper", "mid", "lower"]},
    "macd": {"label": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}, "comps": ["dif", "dea", "hist"]},
    "mom": {"label": "动量（N 日收益）", "params": {"n": 60}, "comps": [""]},
    "atr": {"label": "ATR 波动", "params": {"n": 14}, "comps": [""]},
    "vol_ratio": {"label": "量比（成交量/N 日均量）", "params": {"n": 5}, "comps": [""]},
}
OPS = {"gt": ">", "lt": "<", "ge": ">=", "le": "<=", "cross_above": "上穿", "cross_below": "下穿"}

#: 规则 spec 的合法键——表单/spec/存档/代码导出四条链路的**单一事实源**。
#: 以前 kind 分派散在 UI 四处，GenericRuleStrategy 又用 **_ignored 静默吞掉未知键：
#: 存档 JSON 里拼写错/废弃字段会无声改变回放语义。现在 validate/构造都认这份清单。
SPEC_KEYS = ("symbols", "entry", "exit", "target_weight", "stop_loss_pct", "take_profit_pct", "trailing_pct")


def _validate_operand(op: dict, tag: str) -> List[str]:
    problems: List[str] = []
    t = op.get("t")
    if t == "const":
        if not isinstance(op.get("v"), (int, float)):
            problems.append(f"{tag}: const 缺少数值 v")
    elif t == "field":
        if op.get("f") not in FIELDS:
            problems.append(f"{tag}: 未知价格字段 {op.get('f')!r}")
    elif t == "ind":
        meta = INDICATORS.get(op.get("i", ""))
        if meta is None:
            problems.append(f"{tag}: 未知指标 {op.get('i')!r}")
        else:
            for pk in op.get("p", {}):
                if pk not in meta["params"]:
                    problems.append(f"{tag}: 指标 {op['i']} 无参数 {pk!r}")
            if op.get("c") and op["c"] not in meta["comps"]:
                problems.append(f"{tag}: 指标 {op['i']} 无组件 {op['c']!r}")
    else:
        problems.append(f"{tag}: 未知 operand 类型 {t!r}")
    return problems


def validate_spec(spec: dict, *, strict: bool = True) -> List[str]:
    """校验规则 spec。strict=True 时未知键直接 raise（拼写错/废弃字段不得静默吞掉）。

    返回问题清单（非 strict 模式供 UI 展示）；全部合法时为空。
    """
    problems: List[str] = []
    unknown = [k for k in spec if k not in SPEC_KEYS]
    if unknown:
        msg = f"spec 含未知键 {unknown}（合法键：{list(SPEC_KEYS)}）——旧存档字段演进请显式迁移，不要静默回放"
        if strict:
            raise ValueError(msg)
        problems.append(msg)
    if spec.get("entry") is not None and not isinstance(spec["entry"], dict):
        problems.append("entry 必须是 rule dict")
    for tag in ("entry", "exit"):
        rule = spec.get(tag) or {}
        if rule.get("mode", "all") not in ("all", "any"):
            problems.append(f"{tag}: mode 必须是 all/any")
        for j, c in enumerate(rule.get("conds", [])):
            if c.get("op") not in OPS:
                problems.append(f"{tag}[{j}]: 未知操作符 {c.get('op')!r}")
            problems += _validate_operand(c.get("left") or {}, f"{tag}[{j}].left")
            problems += _validate_operand(c.get("right") or {}, f"{tag}[{j}].right")
    for k in ("target_weight", "stop_loss_pct", "take_profit_pct", "trailing_pct"):
        v = spec.get(k)
        if v is not None and not isinstance(v, (int, float)):
            problems.append(f"{k} 必须是数字")
    if problems and strict:
        raise ValueError("；".join(problems))
    return problems


def operand_to_widget_keys(prefix: str, op: dict) -> dict:
    """operand dict → 表单 widget 键值（spec→表单回填的纯函数部分，从 UI 收编）。

    返回 {f"{prefix}_kind": 展示名, ...}；UI 只负责把结果写进 session_state，
    映射规则本身在这里接受单测。kind 展示串格式与表单 selectbox 选项保持一致：
    「指标label（key）」。常数/价格字段亦同。
    """
    out: Dict[str, object] = {}
    if op.get("t") == "const":
        out[prefix + "_kind"] = "常数"
        out[prefix + "_v"] = float(op["v"])
        return out
    if op.get("t") == "field":
        out[prefix + "_kind"] = "价格字段"
        out[prefix + "_f"] = op["f"]
        return out
    meta = INDICATORS[op["i"]]
    out[prefix + "_kind"] = f"{meta['label']}（{op['i']}）"
    for k, v in op.get("p", {}).items():
        out[f"{prefix}_{k}"] = float(v)
    if meta["comps"] != [""]:
        out[prefix + "_comp"] = op.get("c") or "mid"
    return out


def spec_to_form_state(spec: dict) -> Dict[str, object]:
    """规则 spec → 全部表单 widget 键值（纯函数；UI 应用到 session_state）。

    键名与 web 表单对齐：b_symbols / entry_mode / exit_mode / n_entry / n_exit /
    {tag}_op{i} / {tag}_l{i}_* / {tag}_r{i}_* / sl_weight / sl_stop / sl_tp / sl_trail。
    """
    ent, exi = spec.get("entry") or {}, spec.get("exit") or {}
    state: Dict[str, object] = {
        "b_symbols": list(spec.get("symbols", [])),
        "entry_mode": "满足全部" if ent.get("mode", "all") == "all" else "满足任一",
        "exit_mode": "满足任一" if exi.get("mode", "any") == "any" else "满足全部",
        "n_entry": len(ent.get("conds", [])),
        "n_exit": len(exi.get("conds", [])),
        "sl_weight": float(spec.get("target_weight", 0.95)),
        "sl_stop": float(spec.get("stop_loss_pct", 0.0)) * 100,
        "sl_tp": float(spec.get("take_profit_pct", 0.0)) * 100,
        "sl_trail": float(spec.get("trailing_pct", 0.0)) * 100,
    }
    for tag, rule in (("e", ent), ("x", exi)):
        for i, c in enumerate(rule.get("conds", [])):
            state[f"{tag}_op{i}"] = c["op"]
            state.update(operand_to_widget_keys(f"{tag}_l{i}", c["left"]))
            state.update(operand_to_widget_keys(f"{tag}_r{i}", c["right"]))
    return state


# ------------------------------------------------------------------ 指标计算
def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


class IND:
    """公开指标函数库：输入历史 bar DataFrame，返回 Series 或组件 dict。
    既被 GenericRuleStrategy 使用，也在 generate_python_code 导出的代码里引用。"""

    @staticmethod
    def sma(h: pd.DataFrame, n=20):
        return h["close"].rolling(int(n)).mean()

    @staticmethod
    def ema(h: pd.DataFrame, n=20):
        return h["close"].ewm(span=int(n), adjust=False).mean()

    @staticmethod
    def rsi(h: pd.DataFrame, n=14):
        return _rsi(h["close"], int(n))

    @staticmethod
    def boll(h: pd.DataFrame, n=20, k=2.0):
        mid = h["close"].rolling(int(n)).mean()
        sd = h["close"].rolling(int(n)).std()
        return {"upper": mid + float(k) * sd, "mid": mid, "lower": mid - float(k) * sd}

    @staticmethod
    def donchian(h: pd.DataFrame, n=20):
        up = h["high"].rolling(int(n)).max()
        lo = h["low"].rolling(int(n)).min()
        return {"upper": up, "lower": lo, "mid": (up + lo) / 2}

    @staticmethod
    def macd(h: pd.DataFrame, fast=12, slow=26, signal=9):
        dif = h["close"].ewm(span=int(fast), adjust=False).mean() - h["close"].ewm(span=int(slow), adjust=False).mean()
        dea = dif.ewm(span=int(signal), adjust=False).mean()
        return {"dif": dif, "dea": dea, "hist": (dif - dea) * 2}

    @staticmethod
    def mom(h: pd.DataFrame, n=60):
        return h["close"].pct_change(int(n))

    @staticmethod
    def atr(h: pd.DataFrame, n=14):
        close, high, low = h["close"], h["high"], h["low"]
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        return tr.rolling(int(n)).mean()

    @staticmethod
    def vol_ratio(h: pd.DataFrame, n=5):
        return h["volume"] / h["volume"].rolling(int(n)).mean()


def _compute(op: dict, df: pd.DataFrame) -> Optional[pd.Series]:
    """在历史 bar DataFrame 上计算操作数序列（NaN 前缀保留，交叉判定用最后两点）。"""
    t = op.get("t")
    if t == "field":
        return df[op["f"]] if op["f"] in df.columns else None
    if t == "ind":
        i, p, c = op["i"], op.get("p", {}), op.get("c", "")
        fn = getattr(IND, i, None)
        if fn is None:
            return None
        out = fn(df, **{k: v for k, v in p.items() if k in INDICATORS[i]["params"]})
        if isinstance(out, dict):
            return out.get(c or "mid")
        return out
    return None


def _needed_bars(*ops: dict) -> int:
    need = 30
    for op in ops:
        if op.get("t") != "ind":
            continue
        p = op.get("p", {})
        i = op["i"]
        if i == "macd":
            need = max(need, int(p.get("slow", 26)) + int(p.get("signal", 9)) * 3 + 5)
        elif i == "rsi":
            need = max(need, int(p.get("n", 14)) * 3 + 5)
        else:
            need = max(need, int(p.get("n", 20) or 20) + 5)
    return need


def _collect_ops(spec: dict) -> List[dict]:
    ops: List[dict] = []
    for rule_key in ("entry", "exit"):
        for cond in (spec.get(rule_key) or {}).get("conds", []):
            ops.extend([cond["left"], cond["right"]])
    return ops


def _key(op: dict) -> str:
    return json.dumps(op, sort_keys=True)


def _series(op: dict, df: pd.DataFrame, cache: Dict[str, object]) -> object:
    if op.get("t") == "const":
        return float(op["v"])
    k = _key(op)
    if k not in cache:
        cache[k] = _compute(op, df)
    return cache[k]


def _last2(v):
    """返回 (前一日值, 最新值)；NaN 不足 2 个有效点时给 None。"""
    if _is_scalar(v):
        return float(v), float(v)
    if v is None or len(v.dropna()) < 2:
        return None, None
    return float(v.iloc[-2]), float(v.iloc[-1])


def _is_scalar(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def last_val(x) -> float:
    """公开工具：取序列最新值（导出代码里引用）。"""
    return float(x) if _is_scalar(x) else float(x.iloc[-1])


def cross(a, b, above=True) -> bool:
    """公开工具：判断 a 对 b 的金叉/死叉（最新两根 bar）。"""
    a1, a0 = (float(a), float(a)) if _is_scalar(a) else (float(a.iloc[-2]), float(a.iloc[-1]))
    b1, b0 = (float(b), float(b)) if _is_scalar(b) else (float(b.iloc[-2]), float(b.iloc[-1]))
    return (a1 <= b1 and a0 > b0) if above else (a1 >= b1 and a0 < b0)


def _eval_cond(cond: dict, df: pd.DataFrame, cache: Dict[str, object]) -> bool:
    lv = _series(cond["left"], df, cache)
    rv = _series(cond["right"], df, cache)
    if lv is None or rv is None:
        return False
    l1, l0 = _last2(lv)   # l1=昨日, l0=今日（序列最新点，即昨收）
    r1, r0 = _last2(rv)
    if l1 is None or r1 is None:
        return False
    op = cond["op"]
    if op == "gt":
        return l0 > r0
    if op == "lt":
        return l0 < r0
    if op == "ge":
        return l0 >= r0
    if op == "le":
        return l0 <= r0
    if op == "cross_above":
        return l1 <= r1 and l0 > r0
    if op == "cross_below":
        return l1 >= r1 and l0 < r0
    raise ValueError(f"未知操作符 {op}")


def _rule_ok(rule: Optional[dict], df: pd.DataFrame, cache: Dict[str, object]) -> bool:
    if not rule or not rule.get("conds"):
        return False
    results = [_eval_cond(c, df, cache) for c in rule["conds"]]
    return all(results) if rule.get("mode", "all") == "all" else any(results)


# ------------------------------------------------------------------ 策略
class GenericRuleStrategy(Strategy):
    """多标的独立择时：入场规则命中 → 买到 target_weight；出场规则/止损/止盈/移动止损 → 清仓。

    spec 为纯 dict（见模块 docstring），UI 表单直接构造，json 可存档回放。
    """

    name = "rule_based"

    def __init__(
        self,
        symbols: List[str],
        entry: Optional[dict] = None,
        exit: Optional[dict] = None,
        target_weight: float = 0.95,
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        trailing_pct: float = 0.0,
        strict: bool = True,
        **extras,
    ):
        # 未知键不再静默吞掉：strict 时 validate_spec 直接 raise（旧存档迁移需显式改）。
        validate_spec({**extras, "symbols": symbols, "entry": entry, "exit": exit,
                       "target_weight": target_weight, "stop_loss_pct": stop_loss_pct,
                       "take_profit_pct": take_profit_pct, "trailing_pct": trailing_pct},
                      strict=strict)
        self.symbols = list(symbols)
        self.entry = entry or {"mode": "all", "conds": []}
        self.exit = exit or {"mode": "any", "conds": []}
        self.target_weight = float(target_weight)
        self.stop_loss_pct = float(stop_loss_pct)
        self.take_profit_pct = float(take_profit_pct)
        self.trailing_pct = float(trailing_pct)
        self._peak: Dict[str, float] = {}
        self._look = max(_needed_bars(*_collect_ops({"entry": self.entry, "exit": self.exit})), 30)
        self.params = {
            "symbols": self.symbols, "target_weight": self.target_weight,
            "stop_loss_pct": self.stop_loss_pct, "take_profit_pct": self.take_profit_pct,
            "trailing_pct": self.trailing_pct,
        }

    def on_bar(self, ctx) -> None:
        for s in self.symbols:
            if s not in ctx.universe:  # 当日无有效行情（停牌等）
                continue
            holding = ctx.holding(s)
            px_now = ctx.price(s)
            if holding and px_now:
                cost = ctx.position(s).avg_cost
                peak = max(self._peak.get(s, cost), px_now)
                self._peak[s] = peak
                why = None
                if self.stop_loss_pct and px_now <= cost * (1 - self.stop_loss_pct):
                    why = f"止损 {self.stop_loss_pct:.0%}"
                elif self.take_profit_pct and px_now >= cost * (1 + self.take_profit_pct):
                    why = f"止盈 {self.take_profit_pct:.0%}"
                elif self.trailing_pct and px_now <= peak * (1 - self.trailing_pct):
                    why = f"移动止损 {self.trailing_pct:.0%}"
                if why:
                    ctx.sell(s, quantity=holding, reason=why)
                    self._peak.pop(s, None)
                    continue
            if any(o.symbol == s for o in ctx.pending):
                continue
            hist = ctx.history(s, self._look)
            if hist is None or len(hist) < self._look * 0.6:
                continue
            cache: Dict[str, object] = {}
            if holding:
                if not self._peak.get(s):
                    self._peak[s] = px_now or self._peak.get(s, 0)
                if _rule_ok(self.exit, hist, cache) and (exit_reason := _describe_rule(self.exit)):
                    ctx.sell(s, quantity=holding, reason=f"出场：{exit_reason}")
                    self._peak.pop(s, None)
            else:
                if _rule_ok(self.entry, hist, cache) and (entry_reason := _describe_rule(self.entry)):
                    ctx.target_percent(s, self.target_weight, reason=f"入场：{entry_reason}")


def _describe_rule(rule: dict) -> str:
    joiner = " 且 " if rule.get("mode", "all") == "all" else " 或 "
    return joiner.join(_operand_name(c["left"]) + " " + OPS[c["op"]] + " " + _operand_name(c["right"]) for c in rule["conds"])


def _operand_name(op: dict) -> str:
    if op.get("t") == "const":
        return f"{op['v']:g}"
    if op.get("t") == "field":
        return op["f"]
    p = op.get("p", {})
    label = INDICATORS[op["i"]]["label"]
    ps = ",".join(f"{k}={v:g}" for k, v in p.items())
    c = op.get("c") or ""
    return f"{label}({ps}{',' + c if c else ''})"


# ------------------------------------------------------------------ 等价代码导出
def generate_python_code(spec: dict) -> str:
    """把规则 spec 翻译成可读、可运行的等价 Python（预览/导出用，保证语法有效）。"""
    ops_lines: List[str] = []
    seen: Dict[str, str] = {}

    def expr(op: dict) -> str:
        if op.get("t") == "const":
            return f"{op['v']:g}"
        if op.get("t") == "field":
            return f'h["{op["f"]}"]'
        key = _key(op)
        if key not in seen:
            p = op.get("p", {})
            import re as _re

            slug = "".join(_re.sub(r"\W", "", str(v)) for v in p.values())
            var = "v_" + op["i"] + slug + (op.get("c") or "")
            seen[key] = var
            arg = ", ".join(f"{k}={v!r}" for k, v in p.items())
            comp = f'["{op.get("c") or "mid"}"]' if op["i"] in ("boll", "donchian", "macd") else ""
            ops_lines.append(f"            {var} = IND.{op['i']}(h{', ' + arg if arg else ''}){comp}")
        return seen[key]

    def cond_str(c: dict) -> str:
        l_e, r_e = expr(c["left"]), expr(c["right"])
        if c["op"].startswith("cross"):
            return f"cross({l_e}, {r_e}, above={c['op'] == 'cross_above'})"
        lw = l_e if c["left"].get("t") == "const" else f"last_val({l_e})"
        rw = r_e if c["right"].get("t") == "const" else f"last_val({r_e})"
        return f"{lw} {OPS[c['op']]} {rw}"

    def rule_str(rule: Optional[dict]) -> str:
        conds = (rule or {}).get("conds") or []
        if not conds:
            return "False"
        j = " and " if (rule or {}).get("mode", "all") == "all" else " or "
        return "(" + j.join(cond_str(c) for c in conds) + ")"

    tw = spec.get("target_weight", 0.95)
    enter_s = rule_str(spec.get("entry"))   # 先求值：会把需要的指标行填进 ops_lines
    leave_s = rule_str(spec.get("exit"))
    look = max(_needed_bars(*_collect_ops({"entry": spec.get("entry"), "exit": spec.get("exit")})), 30)
    minlen = int(look * 0.6)

    risk_tests = []
    if spec.get("stop_loss_pct"):
        risk_tests.append(f"px <= cost * (1 - {spec['stop_loss_pct']!r})")
    if spec.get("take_profit_pct"):
        risk_tests.append(f"px >= cost * (1 + {spec['take_profit_pct']!r})")
    if spec.get("trailing_pct"):
        risk_tests.append(f"px <= peak * (1 - {spec['trailing_pct']!r})")
    risk_names = (
        ["止损"] if spec.get("stop_loss_pct") else []
    ) + (["止盈"] if spec.get("take_profit_pct") else []) + (["移动止损"] if spec.get("trailing_pct") else [])
    risk_comment = "  # " + " 或 ".join(risk_names) if risk_tests else ""

    L = [
        "# 网页搭建策略：与规则表单等价的裸代码，可继续编辑演化（导出自量化模拟平台）",
        "from quant_sim.core.strategy_base import Strategy",
        "from quant_sim.strategies.rule_based import IND, last_val, cross",
        "",
        "class MyStrategy(Strategy):",
        f"    # 标的: {' '.join(spec.get('symbols', []))}｜目标仓位 {tw:.0%}｜风控 {' / '.join(t.replace('px ', '') for t in risk_tests) or '无'}",
        "    def __init__(self):",
        "        self._peak = {}",
        "",
        "    def on_bar(self, ctx):",
        f"        for s in {spec.get('symbols', [])!r}:",
        "            if s not in ctx.universe:  # 当日停牌等无行情",
        "                continue",
        "            holding, px = ctx.holding(s), ctx.price(s)",
        "            if holding and px:  # 风控优先于信号出场（与规则表单一致）",
        "                cost = ctx.position(s).avg_cost",
        "                peak = max(self._peak.get(s, cost), px)",
        "                self._peak[s] = peak",
    ]
    if risk_tests:
        L.append(f"                if {' or '.join('(' + t + ')' for t in risk_tests)}:{risk_comment}")
        L.append("                    ctx.sell(s, quantity=holding, reason='风控出场')")
        L.append("                    self._peak.pop(s, None)")
        L.append("                    continue")
    L += [
        "            if any(o.symbol == s for o in ctx.pending):  # 防重复下单",
        "                continue",
        f"            h = ctx.history(s, {look})   # 截至昨收，防未来函数",
        f"            if h is None or len(h) < {minlen}:",
        "                continue",
        *ops_lines,
        f"            enter = {enter_s}",
        f"            leave = {leave_s}",
        "            if holding:",
        "                if leave:",
        "                    ctx.sell(s, quantity=holding, reason='出场')",
        "                    self._peak.pop(s, None)",
        "            elif enter:",
        f"                ctx.target_percent(s, {tw!r}, reason='入场')",
    ]
    return "\n".join(L)
