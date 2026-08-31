# -*- coding: utf-8 -*-
"""校验快照。全过返回 0，任一不过返回非 0 并打印原因。

用法：
    python backend/check_snapshot.py data/20260805.json

校验规则（任务书冻结）：
- date 字段匹配文件名
- 每模块有 status，且 status 只允许 ok/error
- status=ok 的模块 data 非空且关键字段在
- error 模块：error 必须含真实异常关键字之一
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from modules import MODULES, ALLOW_EMPTY

_ERR_KEYWORDS = ("Proxy", "Timeout", "Connection", "Error",
                "RemoteDisconnected", "empty", "None")


def _check_fields(name, data, errors):
    if name == "market_indices":
        if len(data) < 3:
            errors.append("market_indices 指数数 < 3")
        if data:
            for key in ("名称", "最新价", "涨跌幅"):
                if key not in data[0]:
                    errors.append(f"market_indices 缺字段 {key}")
    elif name == "limit_up_pool":
        if len(data) < 1:
            errors.append("limit_up_pool 行数 < 1")
        if data and "连板数" not in data[0]:
            errors.append("limit_up_pool 缺字段 连板数")
    elif name == "limit_break_pool":
        if data and "炸板次数" not in data[0]:
            errors.append("limit_break_pool 缺字段 炸板次数")
    elif name == "hot_stock":
        if data and "排名" not in data[0]:
            errors.append("hot_stock 缺字段 排名")
    elif name == "etf":
        if len(data) < 3:
            errors.append(f"etf 行数 {len(data)} < 3")
        if data and ("名称" not in data[0] or "涨跌幅" not in data[0]):
            errors.append("etf 缺字段 名称/涨跌幅")
    elif name == "boards":
        if len(data) < 10:
            errors.append(f"boards 行数 {len(data)} < 10")
        if data:
            for key in ("名称", "涨跌幅", "history"):
                if key not in data[0]:
                    errors.append(f"boards 缺字段 {key}")
    elif name == "breadth":
        if not isinstance(data, dict) or "上涨" not in data:
            errors.append("breadth 缺 上涨 字段")
    elif name == "lhb":
        if data and "龙虎榜净买额" not in data[0]:
            errors.append("lhb 缺字段 龙虎榜净买额")
    elif name == "concepts":
        if len(data) < 20:
            errors.append(f"concepts 行数 {len(data)} < 20")
        if data and "涨跌幅" not in data[0]:
            errors.append("concepts 缺字段 涨跌幅")
    elif name == "extra":
        if not isinstance(data, dict):
            errors.append("extra data 不是 dict")
        else:
            if len(data.get("distribution", [])) < 9:
                errors.append("extra 涨跌幅分布区间 < 9（应为 9 区间）")
            if len(data.get("popular", [])) < 10:
                errors.append("extra 人气榜 < 10")
    elif name == "global_market":
        if not isinstance(data, dict) or not (data.get("港股") or data.get("美股")):
            errors.append("global_market 港股美股均为空")
    elif name == "speculation":
        if not isinstance(data, dict):
            errors.append("speculation data 不是 dict")
        elif not any(k in data for k in ("themes", "deviation", "cycle", "rules")):
            errors.append("speculation 缺 themes/deviation/cycle/rules")
    elif name == "regulatory":
        if not isinstance(data, dict):
            errors.append("regulatory data 不是 dict")
        else:
            for key in ("abnormal_wave", "penalty"):
                if key not in data:
                    errors.append(f"regulatory 缺分类 {key}")
            if not any(data.get(k) for k in ("abnormal_wave", "penalty")):
                errors.append("regulatory 全部分类为空")


def main():
    if len(sys.argv) < 2:
        print("用法: python check_snapshot.py <snapshot.json>", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    # 兼容 gzip 归档（.json.gz）
    if path.endswith(".json.gz"):
        import gzip
        with gzip.open(path, "rt", encoding="utf-8") as f:
            snap = json.load(f)
    else:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)

    errors = []
    fname_date = os.path.basename(path).replace(".json.gz", "").replace(".json", "")

    if snap.get("date") != fname_date:
        errors.append(f"date({snap.get('date')}) 与文件名({fname_date})不一致")

    modules = snap.get("modules", {})
    # 模块清单单一来源：modules.py registry
    if len(modules) < len(MODULES) - 1:
        errors.append(f"模块数 {len(modules)} < {len(MODULES) - 1}")

    # 允许为空的模块由 registry 的 allow_empty 决定（无跌停/无龙虎榜/无炸板是正常市场状态）
    empty_ok = tuple(k for k, v in ALLOW_EMPTY.items() if v)

    for name, mod in modules.items():
        if not isinstance(mod, dict) or "status" not in mod:
            errors.append(f"{name}: 缺 status 字段")
            continue
        st = mod["status"]
        if st not in ("ok", "error"):
            errors.append(f"{name}: status 非法 {st!r}")
            continue
        if st == "ok":
            data = mod.get("data")
            # limit_down_pool / lhb / limit_break_pool 允许为空：无跌停、无龙虎榜、无炸板是正常市场状态
            if not data and name not in empty_ok:
                errors.append(f"{name}: ok 但 data 为空")
            elif data:
                _check_fields(name, data, errors)
        else:
            err = mod.get("error", "")
            if not any(k in err for k in _ERR_KEYWORDS):
                errors.append(f"{name}: error 缺少真实异常关键字: {err[:80]!r}")

    if errors:
        print("校验失败:")
        for e in errors:
            print(" -", e)
        sys.exit(1)

    print("校验通过: date/模块数/关键字段/异常关键字全部 OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
