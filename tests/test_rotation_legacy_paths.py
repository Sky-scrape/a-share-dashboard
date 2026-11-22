# -*- coding: utf-8 -*-
"""东财 LEGACY 四件套的路径 / 显式开关 / 清理 opt-in 固化（不触网、不写 data/）。

对应复盘《docs/诊断-项目深度复盘-20261121.md》[历史-H-01]（fetch_day 退役后仍写现行
daily/.status）与 L3 前置（LEGACY 删除前先修输出与文档）。

纪律：
- 只读模块常量与源码文本，不调用任何抓取函数，不创建/删除任何文件；
- 防同名模块顶替：`config` 必须是 backend/rotation/config.py（tests/conftest.py 会把
  ROOT/backend 等目录放进 sys.path，别处的 config 若被先导入会静默换掉本组断言）；
- 不引用 `config.DAILY_DIR`——config.py 没有这个常量，引用会 AttributeError。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROTATION_DIR = os.path.join(ROOT, "backend", "rotation")
if ROTATION_DIR not in sys.path:
    sys.path.insert(0, ROTATION_DIR)

import config            # noqa: E402
import fetch_day         # noqa: E402
import backfill_rotation  # noqa: E402

ARCHIVE_DIR = os.path.join(config.DATA_DIR, "daily_legacy_eastmoney")


def test_config_module_is_rotation_config():
    """守卫：import 到的 config 必须是 backend/rotation/config.py（防同名模块顶替）。"""
    got = os.path.normcase(os.path.abspath(config.__file__))
    want_suffix = os.path.normcase(os.path.join("backend", "rotation", "config.py"))
    if not got.endswith(want_suffix):
        pytest.fail(f"config 被同名模块顶替：{got}（期望以 {want_suffix} 结尾）")


def test_archive_dirs_no_longer_write_current_daily():
    """fetch_day / backfill_rotation 的 DAILY_DIR 都指向归档目录，不再写现行 daily/。"""
    assert fetch_day.DAILY_DIR == ARCHIVE_DIR
    assert backfill_rotation.DAILY_DIR == ARCHIVE_DIR
    assert os.sep + "daily" + os.sep not in fetch_day.DAILY_DIR
    assert os.sep + "daily" + os.sep not in backfill_rotation.DAILY_DIR


def test_status_and_boards_files_are_legacy_scoped():
    """状态/清单落 LEGACY 专用文件，不复用 ths_collect 的 .status/rotation.json 与 boards.json。"""
    assert fetch_day.STATUS_PATH.endswith("rotation_legacy.json")
    assert os.path.dirname(fetch_day.STATUS_PATH) == os.path.join(config.PROJECT_ROOT, ".status")
    assert config.BOARDS_FILE.endswith("boards_legacy.json")

    for p in (fetch_day.STATUS_PATH, config.BOARDS_FILE):
        assert os.sep + "daily" + os.sep not in p


def _read_src(module):
    with open(module.__file__, encoding="utf-8") as f:
        return f.read()


def test_fetch_day_require_legacy_banner(capsys):
    """fetch_day：带 --legacy 返回 True，不带返回 False；两态横幅都写明 LEGACY 与 --legacy。"""
    assert fetch_day._require_legacy(["--legacy"]) is True
    on = capsys.readouterr().out
    assert "LEGACY" in on
    assert "--legacy" in on

    assert fetch_day._require_legacy([]) is False
    off = capsys.readouterr().out
    assert "LEGACY" in off
    assert "--legacy" in off


def test_backfill_require_legacy_banner(capsys):
    """backfill_rotation：同构的 --legacy 开关与横幅。"""
    assert backfill_rotation._require_legacy(["--legacy"]) is True
    on = capsys.readouterr().out
    assert "LEGACY" in on
    assert "--legacy" in on

    assert backfill_rotation._require_legacy([]) is False
    off = capsys.readouterr().out
    assert "LEGACY" in off
    assert "--legacy" in off


def test_config_require_legacy_banner(capsys):
    """config.py 的 __main__ 也必须 --legacy；与 fetch_day/backfill 同口径。"""
    assert config._require_legacy(["--legacy"]) is True
    on = capsys.readouterr().out
    assert "LEGACY" in on
    assert "--legacy" in on

    assert config._require_legacy([]) is False
    off = capsys.readouterr().out
    assert "LEGACY" in off
    assert "--legacy" in off


def test_collect_passes_legacy_flag():
    """collect.py 固定给 fetch_day 传 --legacy（否则子进程会以退出码 2 退出）。"""
    import collect  # 只在函数内导入：模块级无副作用，避免污染其它用例

    src = _read_src(collect)
    assert '"--legacy"' in src
    assert "FETCH_DAY" in src


def test_collect_fetch_once_argv_contains_legacy(monkeypatch):
    """collect 传参的实参向量必须带 --legacy（不发子进程，只截获 spawn 参数）。"""
    import collect

    seen = {}

    def fake_spawnv(mode, path, argv):
        seen.update(mode=mode, path=path, argv=list(argv))
        return 0

    monkeypatch.setattr(collect.os, "spawnv", fake_spawnv)
    assert collect._fetch_once() == 0
    assert seen["mode"] == collect.os.P_WAIT
    assert seen["path"] == collect.sys.executable
    assert seen["argv"] == [collect.sys.executable, collect.FETCH_DAY, "--legacy"]
    assert "--legacy" in seen["argv"]


def test_fetch_day_prune_is_opt_in():
    """清理循环只在 --prune 时执行：默认运行绝不删除归档文件。"""
    src = _read_src(fetch_day)
    assert '"--prune"' in src
    assert "if args.prune:" in src
    # os.remove 必须出现在 if args.prune: 之后（即被该分支包裹，而不是无条件执行）
    assert src.index("os.remove(") > src.index("if args.prune:")
