# -*- coding: utf-8 -*-
r"""任务入口检查器（CI 用，2026-11-21 复盘 §10 / 事故-H-03、H-05）。

背景：bat 护栏此前只在 tests/smoke.py:408-416 扫 backend/** 且只查 CRLF——
根目录 bat 无护栏（事故-H-03）；backend/_hidden_runner.vbs 虽已被 git 跟踪，
但没有任何 bat 引用它，一旦从索引移除不会被发现（10-21 事故：换机后计划任务
指向缺失脚本，事故-H-05）。

本脚本做两件事（纯标准库，退出码 0/1/2）：

1. 行尾与编码：backend/**/*.bat + 根目录 *.bat 必须 CRLF、无 BOM；首个非 ASCII
   字符出现之前必须已有一行 `chcp 65001`（cmd 按 ANSI 解释 bat，先切码页再输出
   中文，否则乱码或提前退出——2026-09-03 / 09-30 编码早退事故）。
2. 引用完整性：解析 %PY%/python/py/wscript（以及 `call`）之后的 .py/.vbs 引用，
   按 bat 内 `cd /d "%~dp0..."` 推导工作目录（反斜杠先统一成 os.sep 再
   os.path.normpath，Linux 下也能折叠 `..\..`），断言目标文件存在且
   `git ls-files` 已收录；backend/**/*.vbs + 根目录 *.vbs 独立断言
   「存在且已跟踪」——不依赖是否被 bat 引用，正好补 H-05 的缺口。

用法:
    python tools/check_task_entries.py            # 默认扫仓库根（本文件上两级）
    python tools/check_task_entries.py --no-git   # 只查存在性，跳过 git 跟踪校验
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

BOM_PREFIX = b"\xef\xbb\xbf"
#: bat 里可能直接出现的解释器 token（小写比较；路径残壳如 `%PY%` 见 parse_script_refs）
INTERPRETERS = {"%py%", "python", "python.exe", "py", "py.exe", "wscript", "wscript.exe"}
#: token 两端要剥掉的壳（引号、括号、重定向/连接符残片、管道里包命令的反引号）
_STRIP_CHARS = "\"'()[]{}^&;`"
_SCRIPT_SUFFIXES = (".py", ".vbs")
#: rem / :: / echo 行不参与引用解析
_SKIP_LINE_RE = re.compile(r"^(?:rem\b|::|echo\b)", re.I)
#: bat 内切换工作目录的写法（`cd /d "%~dp0.."`）
_CD_RE = re.compile(r'^cd\s+/d\s+"?([^"]+)"?', re.I)


def _dedupe_sorted(paths):
    keyed = {p.resolve(): p for p in paths if p.is_file()}
    return sorted(keyed.values(), key=lambda p: str(p).lower())


def iter_bats(root):
    """backend/**/*.bat + 根目录 *.bat。"""
    root = Path(root)
    found = list(root.glob("*.bat"))
    if (root / "backend").is_dir():
        found.extend((root / "backend").rglob("*.bat"))
    return _dedupe_sorted(found)


def iter_vbs(root):
    """backend/**/*.vbs + 根目录 *.vbs。"""
    root = Path(root)
    found = list(root.glob("*.vbs"))
    if (root / "backend").is_dir():
        found.extend((root / "backend").rglob("*.vbs"))
    return _dedupe_sorted(found)


def check_bytes(data, name):
    """检查 bat 字节：BOM / CRLF / 非 ASCII 前是否有 chcp 65001。返回违规原因列表。"""
    problems = []
    body = data
    if body.startswith(BOM_PREFIX):
        problems.append(f"{name}:1: 含 UTF-8 BOM（cmd 按 ANSI 读 bat，BOM 会把首行指令弄坏）")
        body = body[len(BOM_PREFIX):]
    lf_total = body.count(b"\n")
    crlf_total = body.count(b"\r\n")
    cr_total = body.count(b"\r")
    if lf_total != crlf_total or cr_total != crlf_total:
        first_bad = 1
        for idx, raw in enumerate(body.split(b"\n")):
            if raw and not raw.endswith(b"\r"):
                first_bad = idx + 1
                break
        problems.append(
            f"{name}:{first_bad}: 行尾非 CRLF（裸 LF {lf_total - crlf_total} 处、"
            f"裸 CR {cr_total - crlf_total} 处）；"
            "cmd 对 LF-only / CR-only 批处理解析错乱、计划任务秒退 255（2026-09-03 事故）")
    lines = body.split(b"\n")
    first_non_ascii = None
    for idx, raw in enumerate(lines):
        if any(byte >= 0x80 for byte in raw):
            first_non_ascii = idx
            break
    if first_non_ascii is not None:
        has_chcp = any(
            lines[idx].strip().lower().startswith(b"chcp 65001")
            for idx in range(first_non_ascii)
        )
        if not has_chcp:
            problems.append(
                f"{name}:{first_non_ascii + 1}: 非 ASCII 字符前没有 `chcp 65001`"
                "（cmd 先按 ANSI 解释，中文会乱码/提前退出）")
    return problems


def parse_script_refs(text):
    """解析 bat 文本里的脚本引用，返回 [(行号, 去引号路径)]。"""
    refs = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        body = raw.strip().lstrip("@").strip()
        if not body or _SKIP_LINE_RE.match(body):
            continue
        tokens = body.split()
        # `call xxx.py` / `call xxx.vbs`：第二 token 是脚本且不含环境变量（%HT_EXE% 不算）
        if len(tokens) > 1 and tokens[0].strip(_STRIP_CHARS).lower() == "call":
            second = tokens[1].strip(_STRIP_CHARS)
            if second.lower().endswith(_SCRIPT_SUFFIXES) and "%" not in second:
                refs.append((lineno, second))
        for idx, token in enumerate(tokens):
            cand = token.strip(_STRIP_CHARS)
            low = cand.lower()
            if not (low in INTERPRETERS or low.startswith("%py%")):
                continue
            for nxt in tokens[idx + 1:]:
                arg = nxt.strip(_STRIP_CHARS)
                if not arg:
                    continue
                if arg.startswith("-") or arg.startswith("/"):
                    continue  # 解释器选项（-m / -c / /min ...）
                if arg.lower().endswith(_SCRIPT_SUFFIXES):
                    refs.append((lineno, arg))
                    break
                if arg in ("&&", "||", "|", ">", ">>", "<", "&"):
                    break  # 命令到此结束，别把同一行后面的文本当参数
    return refs


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def bat_cwd(bat):
    """按 bat 内 `cd /d "%~dp0.."` 推导工作目录，初值 bat.parent。"""
    bat = Path(bat)
    cwd = bat.parent
    for raw in _read_text(bat).splitlines():
        line = raw.strip().lstrip("@").strip()
        if not line or _SKIP_LINE_RE.match(line):
            continue
        match = _CD_RE.match(line)
        if not match:
            continue
        raw_path = match.group(1).replace("%~dp0", str(bat.parent) + os.sep)
        raw_path = raw_path.replace("\\", os.sep)
        cwd = Path(os.path.normpath(raw_path))
    return cwd


def resolve_ref(bat, cwd, ref):
    r"""把 bat 内的脚本引用解析成路径；cwd=None 时按 bat 内 cd /d 推导。

    反斜杠先统一成 os.sep 再 normpath——这样 Windows 写的 `..\..` 在 Linux
    上也能正确折叠（CI 有 ubuntu 腿）。
    """
    bat = Path(bat)
    if cwd is None:
        cwd = bat_cwd(bat)
    raw = ref.strip().strip("\"'")
    raw = raw.replace("%~dp0", str(bat.parent) + os.sep)
    path = raw.replace("\\", os.sep)
    if os.path.isabs(path):
        return Path(os.path.normpath(path))
    return Path(os.path.normpath(os.path.join(str(cwd), path)))


def _rel(path, root):
    try:
        rel = os.path.relpath(str(path), str(root))
    except ValueError:  # Windows 跨盘符无法相对化
        rel = str(path)
    return rel.replace(os.sep, "/")


def _track_key(path):
    """git ls-files 与本地路径的统一比较键（Windows 大小写/斜杠不敏感）。"""
    return os.path.normcase(str(path).replace("/", os.sep))


def _norm_tracked(tracked):
    return None if tracked is None else {_track_key(item) for item in tracked}


def scan_bat(path, tracked, root):
    """检查单个 bat：行尾/编码 + 引用存在且已跟踪。返回违规原因列表。"""
    path = Path(path)
    root = Path(root)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"{_rel(path, root)}: 无法读取（{exc}）"]
    problems = list(check_bytes(data, _rel(path, root)))
    keys = _norm_tracked(tracked)
    cwd = bat_cwd(path)
    for lineno, ref in parse_script_refs(data.decode("utf-8", errors="replace")):
        target = resolve_ref(path, cwd, ref)
        if not target.is_file():
            problems.append(f"{_rel(path, root)}:{lineno}: 引用不存在：{ref} → {_rel(target, root)}")
            continue
        if keys is not None:
            rel = _rel(target, root)
            if _track_key(rel) not in keys:
                problems.append(
                    f"{_rel(path, root)}:{lineno}: 引用未跟踪：{ref}"
                    f"（git ls-files 无 {rel}；换机会指向缺失脚本）")
    return problems


def scan_vbs(path, tracked, root):
    """检查单个 .vbs：存在 + git 已跟踪（不解析内容，也不依赖 bat 是否引用）。"""
    path = Path(path)
    root = Path(root)
    rel = _rel(path, root)
    if not path.is_file():
        return [f"{rel}: 不存在"]
    keys = _norm_tracked(tracked)
    if keys is not None and _track_key(rel) not in keys:
        return [f"{rel}: vbs 未跟踪（git ls-files 无此文件；即使无任何 bat 引用也必须入库——10-21 事故）"]
    return []


def _git_tracked(root):
    """返回 git ls-files 的路径列表；git 不可用/不是仓库返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "surrogateescape")
    return [item for item in out.split("\0") if item]


def main(argv=None):
    parser = argparse.ArgumentParser(description="任务入口检查器：bat/vbs 行尾、BOM、编码 + 引用存在且已入库")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="仓库根（默认本文件上两级）")
    parser.add_argument("--no-git", action="store_true",
                        help="跳过 git 跟踪校验（只查文件存在性）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    tracked = None
    if not args.no_git:
        tracked = _git_tracked(root)
        if tracked is None:
            print("[task-entries] git 不可用或该目录不是 git 仓库，无法校验「已入库」；"
                  "只查文件存在性请加 --no-git", file=sys.stderr)
            return 2

    bats = iter_bats(root)
    vbs = iter_vbs(root)
    problems = []
    ref_count = 0
    for bat in bats:
        ref_count += len(parse_script_refs(_read_text(bat)))
        problems.extend(scan_bat(bat, tracked, root))
    for item in vbs:
        problems.extend(scan_vbs(item, tracked, root))

    if problems:
        for problem in problems:
            print(problem)
        print(f"[task-entries] FAIL：{len(problems)} 条违规 / {len(bats)} bat / {len(vbs)} vbs")
        return 1
    print(f"[task-entries] OK：{len(bats)} bat / {len(vbs)} vbs / {ref_count} 脚本引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
