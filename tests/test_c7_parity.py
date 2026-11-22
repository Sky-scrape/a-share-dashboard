# -*- coding: utf-8 -*-
"""C7 双份实现对拍（复盘 §6 新人-U-01 / §9 X3）。

生产侧 backend/recap/spec_pool.py 与研究侧 strategy-iter/engine/rules.py 各手写一份
C7 口径（版本 / 隔夜美股闸门 / NLU 市场量能闸门 / 入选门槛）。此前没有对拍，改错
其中一份不会有任何红灯。本文件只证明两份实现「同值」，不证明 C7 规格本身正确
（2026-11-03 的教训：同值对拍防漂移，不替代对策略口径正确性的独立论证）。

全部离线、不读 data/recap：门槛用例把 spec_validate.POOL_OPT monkeypatch 到
pytest 的 tmp_path（文件不存在 / 临时状态文件两种形态）。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategy-iter"))

from engine import rules as engine_rules  # noqa: E402

import spec_pool  # noqa: E402
import spec_validate  # noqa: E402


def _c7():
    """rules.py 的 C7 口径；结构缺失时直接失败并提示可能是 rules.py 变了。"""
    c7 = getattr(engine_rules, "C7", None)
    if not isinstance(c7, dict):
        pytest.fail("strategy-iter/engine/rules.py 缺少 C7（rules.py 结构变了？）")
    return c7


def _group(c7, name):
    grp = c7.get(name)
    if not isinstance(grp, dict):
        pytest.fail(f"rules.C7['{name}'] 缺失或不是 dict（rules.py 结构变了？）")
    return grp


def _min_score(c7, group):
    grp = _group(c7, group)
    if "min_score" not in grp:
        pytest.fail(f"rules.C7['{group}']['min_score'] 缺失（rules.py 结构变了？）")
    return grp["min_score"]


def test_rules_version():
    c7 = _c7()
    if "version" not in c7:
        pytest.fail("rules.C7['version'] 缺失（rules.py 结构变了？）")
    assert spec_pool.RULES_VERSION == c7["version"]


def test_us_gate():
    regime = _group(_c7(), "regime")
    if "us_night_force_defensive" not in regime:
        pytest.fail("rules.C7['regime']['us_night_force_defensive'] 缺失（rules.py 结构变了？）")
    assert tuple(spec_pool._MF_US_GATE) == tuple(regime["us_night_force_defensive"])


def test_amt_ratio_gate():
    nlu = _group(_c7(), "nlu_group")
    if "market_amt_ratio_min" not in nlu:
        pytest.fail("rules.C7['nlu_group']['market_amt_ratio_min'] 缺失（rules.py 结构变了？）")
    assert spec_pool._NLU_AMT_RATIO_MIN == nlu["market_amt_ratio_min"] == 0.9


def test_min_score_default(monkeypatch, tmp_path):
    """状态文件缺失 → C7 默认门槛，且与研究侧 C7 两组 min_score 同值。"""
    monkeypatch.setattr(spec_validate, "POOL_OPT", str(tmp_path / "absent.json"))
    opt = spec_validate._load_opt()
    c7 = _c7()
    assert opt["min_score"]["lu"] == _min_score(c7, "lu_group") == 70.0
    assert opt["min_score"]["nlu"] == _min_score(c7, "nlu_group") == 70.0


def test_min_score_from_state(monkeypatch, tmp_path):
    """C7 线状态文件带入旋钮；旧 C_Final 线（门槛 55）不得带入，回默认 70/70。"""
    p = tmp_path / "pool_opt.json"
    p.write_text(json.dumps({"version": "C7+o1",
                             "min_score": {"lu": 72.0, "nlu": 71.0}}),
                 encoding="utf-8")
    monkeypatch.setattr(spec_validate, "POOL_OPT", str(p))
    opt = spec_validate._load_opt()
    assert (opt["min_score"]["lu"], opt["min_score"]["nlu"]) == (72.0, 71.0)

    p.write_text(json.dumps({"version": "C_Final",
                             "min_score": {"lu": 55.0}}),
                 encoding="utf-8")
    opt = spec_validate._load_opt()
    assert (opt["min_score"]["lu"], opt["min_score"]["nlu"]) == (70.0, 70.0)
