# -*- coding: utf-8 -*-
"""snapio 日期键归一化回归（20261121 深度复盘 §8 根因 2 / §10 / 事故-H-02）。

覆盖：
- normalize_date8：8 位原样、ISO 归一化 + warning（含原值）、其它形状 ValueError；
- save/load/snap_path 三入口都走归一化（ISO 键不再读 None / 写成 2026-10-20.json）；
- 缺失 → None；损坏明文/gzip → None + warning（不抛）；
- (mtime,size) FIFO 缓存语义不变；
- 全部读写显式指向 tmp_path，收尾断言真实 data/recap 未被写入。
"""
import gzip
import json
import os

import pytest

import snapio


class _FakeLog(object):
    """收集 warning 调用（monkeypatch snapio.LOG，不依赖 logging 配置）。"""

    def __init__(self):
        self.calls = []

    def warning(self, msg, *args):
        self.calls.append((msg % args) if args else msg)

    def messages(self):
        return "\n".join(str(c) for c in self.calls)


# ---------- normalize_date8 ----------

def test_normalize_valid_8digits_unchanged():
    assert snapio.normalize_date8("20261020") == "20261020"


def test_normalize_iso_returns_8digits_and_warns(monkeypatch):
    log = _FakeLog()
    monkeypatch.setattr(snapio, "LOG", log)
    assert snapio.normalize_date8("2026-10-20") == "20261020"
    msgs = log.messages()
    assert "2026-10-20" in msgs          # 原值
    assert "20261020" in msgs            # 归一化结果
    assert "归一化" in msgs


@pytest.mark.parametrize("bad", [
    None, 20261020, "", "2026/10/20", "2026102", "202610201", "2026-10-20 10:00",
])
def test_normalize_invalid_raises_valueerror(bad):
    with pytest.raises(ValueError) as ei:
        snapio.normalize_date8(bad)
    assert repr(bad) in str(ei.value)


# ---------- 三入口归一化 + 往返 ----------

def test_save_load_roundtrip_with_iso_key(tmp_path):
    d = str(tmp_path)
    snapio.save("2026-10-20", {"x": 1}, d)
    assert (tmp_path / "20261020.json").is_file()
    assert not (tmp_path / "2026-10-20.json").exists()
    assert snapio.list_dates(d) == ["20261020"]
    assert snapio.load("2026-10-20", d)["x"] == 1
    # snap_path 也接受 ISO 键
    assert snapio.snap_path(d, "2026-10-20") == str(tmp_path / "20261020.json")


def test_gz_snapshot_visible_and_loadable(tmp_path):
    d = str(tmp_path)
    snapio.save("20261020", {"x": 1}, d)
    with gzip.open(str(tmp_path / "20261021.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"y": 2}, f)
    assert snapio.list_dates(d) == ["20261021", "20261020"]
    assert snapio.load("2026-10-21", d)["y"] == 2


# ---------- 缺失 / 损坏 ----------

def test_missing_returns_none_explicit_dir(tmp_path):
    assert snapio.load("20260101", str(tmp_path)) is None


def test_missing_returns_none_default_dir(tmp_path, monkeypatch):
    # 不传 recap_dir：证明默认 RECAP_DATA 也走同一归一化，且不写盘
    monkeypatch.setattr(snapio, "RECAP_DATA", str(tmp_path))
    assert snapio.load("20260101") is None
    assert not (tmp_path / "20260101.json").exists()


def test_corrupt_json_returns_none_and_warns(tmp_path, monkeypatch):
    log = _FakeLog()
    monkeypatch.setattr(snapio, "LOG", log)
    (tmp_path / "20261022.json").write_text("{bad", encoding="utf-8")
    assert snapio.load("20261022", str(tmp_path)) is None
    assert log.calls, "损坏快照必须记 warning"


def test_corrupt_gz_returns_none_and_warns(tmp_path, monkeypatch):
    log = _FakeLog()
    monkeypatch.setattr(snapio, "LOG", log)
    (tmp_path / "20261023.json.gz").write_bytes(b"not-a-gzip-stream")
    assert snapio.load("20261023", str(tmp_path)) is None
    assert log.calls, "损坏 gzip 快照必须记 warning"


# ---------- 缓存 ----------

def test_load_cache_identity_and_mtime_invalidation(tmp_path):
    d = str(tmp_path)
    snapio.save("20261024", {"v": 1}, d)
    a = snapio.load("20261024", d)
    b = snapio.load("20261024", d)
    assert a is b                      # (mtime,size) 命中：同一对象

    p = tmp_path / "20261024.json"
    snapio.save("20261024", {"v": 2}, d)
    st = os.stat(str(p))
    os.utime(str(p), (st.st_atime + 10, st.st_mtime + 10))  # 制造明确的 mtime 差异
    c = snapio.load("20261024", d)
    assert c["v"] == 2
    assert c is not a


# ---------- 冻结目录保护（放最后） ----------

def test_zz_real_recap_dir_untouched():
    real = os.path.join(snapio.ROOT, "data", "recap")
    assert not os.path.exists(os.path.join(real, "20261020.json"))
