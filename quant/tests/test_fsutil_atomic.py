"""core.fsutil 原子写回归锁（2026-09-11 写盘收敛新增）。

覆盖：JSON 往返与 default/sort_keys 透传、编码与换行语义、失败清理临时文件。
与 backend/fsutil.py 同构实现的产物逐字节兼容。
"""

from __future__ import annotations

import json
import os

import pytest

from quant_sim.core.fsutil import save_json_atomic, save_text_atomic


def test_json_roundtrip_and_dumps_kwargs(tmp_path):
    p = tmp_path / "a.json"
    save_json_atomic(p, {"名": "值", "n": 1}, indent=1)
    assert json.loads(p.read_text(encoding="utf-8")) == {"名": "值", "n": 1}
    d = tmp_path / "d.json"
    save_json_atomic(d, {"b": 1, "a": object()}, default=str, sort_keys=True)
    assert list(json.loads(d.read_text(encoding="utf-8"))) == ["a", "b"]


def test_text_newline_semantics(tmp_path):
    lf = tmp_path / "lf.txt"
    save_text_atomic(lf, "a\nb\n", newline="\n")
    assert lf.read_bytes() == b"a\nb\n"
    native = tmp_path / "native.txt"
    save_text_atomic(native, "a\nb\n")
    assert native.read_bytes() == (b"a\r\nb\r\n" if os.name == "nt" else b"a\nb\n")


def test_cleans_up_tmp_on_write_failure(tmp_path):
    target = tmp_path / "x.txt"
    with pytest.raises(UnicodeEncodeError):
        save_text_atomic(target, "中文\ud800surrogate", encoding="utf-8")
    assert list(tmp_path.iterdir()) == []   # 无残留临时文件，目标也未出现


def test_dotted_path_is_normalized(tmp_path):
    # abspath+normpath 消化 "." 段后照常落盘（等价路径、同一文件）
    p = tmp_path / "real.txt"
    save_text_atomic(os.path.join(str(tmp_path), ".", "real.txt"), "t")
    assert p.read_text(encoding="utf-8") == "t"
