# -*- coding: utf-8 -*-
"""任务入口检查器的单元测试（tools/check_task_entries.py）。

覆盖复盘 §10 / 事故-H-03、H-05 的失败路径：
- check_bytes：LF-only / BOM / 中文无 chcp / 中文在 chcp 后 / chcp 在中文后
- parse_script_refs：现有 10 个 bat 的真实写法（for /f 反引号、start、if not、call、
  `py start.py`）都要解析出 .py；%HT_EXE%、packaging\\*.spec、-m pip 不能当引用
- resolve_ref：按 `cd /d "%~dp0.."` 推导到 tmp_path/backend/backup.py（不是仓库 root），
  `..\\..` 也要能折叠（Windows 反斜杠在 Linux 上同样 normpath）
- scan_bat：引用存在但未跟踪 → 违规；已跟踪 → 通过；引用不存在 → 违规
- scan_vbs：无任何 bat 引用的 .vbs 未跟踪也要报（H-05 的缺口）
- main：仓库根退出 0；空目录 --no-git 退出 0；git 不可用且未 --no-git 退出 2
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "tools" / "check_task_entries.py"

_spec = importlib.util.spec_from_file_location("check_task_entries_under_test", _SCRIPT)
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _write_bat(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- check_bytes

def test_check_bytes_clean_crlf_ascii():
    assert ce.check_bytes(b"@echo off\r\nchcp 65001 >nul\r\necho hi\r\n", "x.bat") == []


def test_check_bytes_lf_only():
    problems = ce.check_bytes(b"@echo off\nchcp 65001 >nul\necho hi\n", "x.bat")
    assert any("CRLF" in p for p in problems)


def test_check_bytes_cr_only():
    problems = ce.check_bytes(b"@echo off\rchcp 65001 >nul\r", "x.bat")
    assert any("CRLF" in p for p in problems)


def test_check_bytes_bom():
    problems = ce.check_bytes(b"\xef\xbb\xbf@echo off\r\nchcp 65001 >nul\r\n", "x.bat")
    assert any("BOM" in p for p in problems)


def test_check_bytes_chinese_without_chcp():
    problems = ce.check_bytes("@echo off\r\ntitle 看板\r\n".encode("utf-8"), "x.bat")
    assert any("chcp" in p.lower() for p in problems)


def test_check_bytes_chinese_after_chcp_ok():
    data = "@echo off\r\nchcp 65001 >nul\r\ntitle 看板\r\n".encode("utf-8")
    assert ce.check_bytes(data, "x.bat") == []


def test_check_bytes_chcp_after_chinese_still_bad():
    data = "@echo off\r\ntitle 看板\r\nchcp 65001 >nul\r\n".encode("utf-8")
    problems = ce.check_bytes(data, "x.bat")
    assert any("chcp" in p.lower() for p in problems)


# --------------------------------------------------------- parse_script_refs

def _ref_paths(refs):
    return {path.replace("\\", "/") for _, path in refs}


def test_parse_script_refs_fetch_task():
    text = (ROOT / "backend" / "recap" / "fetch_task.bat").read_text(encoding="utf-8")
    refs = ce.parse_script_refs(text)
    paths = _ref_paths(refs)
    for expected in ("backend/recap/concept_map.py", "backend/recap/fetch_daily.py",
                     "backend/recap/spec_duckdb.py", "backend/recap/archive.py",
                     "backend/global/fetch_global.py", "backend/notify_once.py"):
        assert expected in paths, (expected, paths)
    # `call "%HT_EXE%" data sync` 是环境变量，不是脚本引用
    assert not any("%" in path for path in paths), paths


def test_parse_script_refs_start_py_variants():
    autostart = ce.parse_script_refs(
        (ROOT / "backend" / "autostart_task.bat").read_text(encoding="utf-8"))
    assert any(path.replace("\\", "/") == "start.py" for _, path in autostart), autostart
    # 打开看板.bat 最后一行是裸 `py start.py`（无引号、无 %PY%）
    launcher = ce.parse_script_refs(
        (ROOT / "打开看板.bat").read_text(encoding="utf-8"))
    assert [path for _, path in launcher] == ["start.py"], launcher


def test_parse_script_refs_ignores_spec_and_option_modules():
    packing = ce.parse_script_refs(
        (ROOT / "打包exe.bat").read_text(encoding="utf-8"))
    paths = _ref_paths(packing)
    # `%PY% -m PyInstaller packaging\ak-dashboard.spec`：.spec 不是 .py
    assert not any(path.lower().endswith(".spec") for path in paths), paths
    # `%PY% -m pip install ...`：pip 不是脚本引用
    assert "pip" not in {path.lower() for path in paths}, paths


# ------------------------------------------------------------- resolve_ref

def test_resolve_ref_uses_bat_cd_not_repo_root(tmp_path):
    backend = tmp_path / "backend"
    bat = backend / "fake.bat"
    _write_bat(bat, '@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0.."\r\n%PY% backend\\backup.py\r\n')
    assert ce.bat_cwd(bat) == tmp_path
    refs = ce.parse_script_refs(bat.read_text(encoding="utf-8"))
    assert refs == [(4, "backend\\backup.py")], refs
    assert ce.resolve_ref(bat, ce.bat_cwd(bat), refs[0][1]) == tmp_path / "backend" / "backup.py"
    # cwd=None 时自己按 cd /d 推导，不应落到仓库 root
    assert ce.resolve_ref(bat, None, "backend\\backup.py") == tmp_path / "backend" / "backup.py"


def test_resolve_ref_folds_dotdot_dotdot(tmp_path):
    bat = tmp_path / "backend" / "fake2.bat"
    _write_bat(bat, '@echo off\r\ncd /d "%~dp0..\\.."\r\n%PY% backend\\backup.py\r\n')
    assert ce.bat_cwd(bat) == tmp_path.parent
    assert ce.resolve_ref(bat, None, "backend\\backup.py") == tmp_path.parent / "backend" / "backup.py"


# ---------------------------------------------------------------- scan_bat

def test_scan_bat_reference_exists_but_untracked(tmp_path):
    bat = tmp_path / "backend" / "x.bat"
    _write_bat(bat, '@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0.."\r\n%PY% backend\\bar.py\r\n')
    (tmp_path / "backend" / "bar.py").write_text("", encoding="utf-8")
    problems = ce.scan_bat(bat, set(), tmp_path)
    assert any("未跟踪" in p for p in problems), problems
    # 引用已入库 → 通过
    assert ce.scan_bat(bat, {"backend/bar.py"}, tmp_path) == []


def test_scan_bat_reference_missing(tmp_path):
    bat = tmp_path / "backend" / "x.bat"
    _write_bat(bat, '@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0.."\r\n%PY% backend\\bar.py\r\n')
    problems = ce.scan_bat(bat, {"backend/bar.py"}, tmp_path)
    assert any("不存在" in p for p in problems), problems


# ---------------------------------------------------------------- scan_vbs

def test_scan_vbs_untracked_even_without_any_bat_reference(tmp_path):
    vbs = tmp_path / "backend" / "x.vbs"
    _write_bat(vbs, "' hidden launcher\r\n")
    problems = ce.scan_vbs(vbs, set(), tmp_path)
    assert any("未跟踪" in p for p in problems), problems
    assert ce.scan_vbs(vbs, {"backend/x.vbs"}, tmp_path) == []


# ---------------------------------------------------------------- main

def _run(args, **kwargs):
    return subprocess.run(
        [sys.executable, str(_SCRIPT)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **kwargs)


def test_main_repo_exit_0():
    proc = _run([], cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[task-entries] OK" in proc.stdout, proc.stdout


def test_main_empty_root_no_git_exit_0(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _run(["--root", str(empty), "--no-git"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[task-entries] OK" in proc.stdout, proc.stdout


def test_main_git_unavailable_exit_2(tmp_path):
    # GIT_DIR 指到不存在的仓库 → git ls-files 必失败，走「未加 --no-git 就退出 2」分支
    env = dict(os.environ)
    env["GIT_DIR"] = str(tmp_path / "no-such-git-dir")
    proc = _run(["--root", str(tmp_path)], env=env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--no-git" in (proc.stdout + proc.stderr), proc.stdout + proc.stderr
