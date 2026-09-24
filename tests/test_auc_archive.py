# -*- coding: utf-8 -*-
"""auc_archive 单测 + F1/F2 修复行为测试（2026-09-25 审查批次）。

用例对应《docs/测试与审查方案-20260925.md》§5.3（N 组：归档模块本体）与 §12
（TC 组：采集器终态路径的归档挂接 + save_status 对 archive 段的保留语义）。

全部离线：数据读写经 monkeypatch 指向 tmp_path，绝不触碰真实 data/auction 与
.status/auction.json（生产机器上有计划任务与在跑服务，见方案 §15 红线）。
"""
import gzip
import json
import os

import pytest

import auc_archive
import auc_config as C
import fsutil

SIX = dict(
    final={"date": "20260925", "round": {"count": 2}},
    series={"date": "20260925", "rounds": [{"label": "09:15:30"}]},
    rounds_meta={"date": "20260925", "rounds": [{"label": "09:15:30"}]},
    benchmark={"rows": [1, 2]},
    sector={"rows": list(range(90))},
    watchmap={"600000.SH": {"i": "银行", "n": "浦发银行", "s": "U"}},
)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """隔离的归档/状态目录：ARCHIVE_DIR、STATUS_JSON 全部指向 tmp。"""
    arch = tmp_path / "archive"
    status = tmp_path / "status.json"
    monkeypatch.setattr(auc_archive, "ARCHIVE_DIR", str(arch))
    monkeypatch.setattr(C, "STATUS_JSON", str(status))
    # build_archive 以 "<name>.json" 调 load；FIX 以短名（"final"）为键
    monkeypatch.setattr(C, "load_json", lambda name: FIX.get(name[:-5]))
    return tmp_path, arch, str(status)


FIX = {}


# ---------------------------------------------------------------- N 组：归档本体

def test_n1_build_archive_all_six(iso):
    FIX.clear(); FIX.update(SIX)
    p = auc_archive.build_archive("20260925")
    assert p["v"] == 1 and p["date"] == "20260925" and p["archived_at"]
    assert set(p["files"]) == set(auc_archive.FILES)


def test_n2_build_archive_partial(iso):
    FIX.clear(); FIX.update(SIX)
    orig = C.load_json

    def missing_sector(name):
        return None if name == "sector.json" else orig(name)

    p = auc_archive.build_archive("20260925", load=missing_sector)
    assert "sector" not in p["files"] and set(p["files"]) == set(SIX) - {"sector"}


def test_n3_archive_empty_day(iso):
    """当日无任何产物：不写文件、ok=False、原因落状态。"""
    FIX.clear()
    tmp_path, arch, status = iso
    res = auc_archive.archive("20260925")
    assert res["ok"] is False and "无竞价产物" in res["error"]
    assert not os.path.exists(res["path"])
    st = json.load(open(status, encoding="utf-8"))
    assert st["archive"]["ok"] is False and "无竞价产物" in st["archive"]["error"]


def test_n4_archive_idempotent_overwrite(iso):
    FIX.clear(); FIX.update(SIX)
    tmp_path, arch, status = iso
    r1 = auc_archive.archive("20260925", quiet=True)
    assert r1["ok"] is True and r1["bytes"] == os.path.getsize(r1["path"])
    # 第二次换内容仍幂等覆盖：同路径单文件、内容为新 payload
    FIX["final"] = {"date": "20260925", "round": {"count": 99}}
    r2 = auc_archive.archive("20260925", quiet=True)
    assert r2["ok"] is True
    assert len([f for f in os.listdir(arch) if f.endswith(".gz")]) == 1
    back = json.load(gzip.open(r2["path"]))
    assert back["files"]["final"]["round"]["count"] == 99


def test_n5_archive_failure_never_raises(iso, monkeypatch):
    """归档失败绝不拖垮采集主流程：错误截断落状态，不向调用方抛。"""
    FIX.clear(); FIX.update(SIX)
    tmp_path, arch, status = iso

    def boom(path, obj, **kw):
        raise RuntimeError("disk gone " * 40)

    monkeypatch.setattr(fsutil, "save_gzip_json_atomic", boom)
    res = auc_archive.archive("20260925", quiet=True)
    assert res["ok"] is False
    assert res["error"].startswith("RuntimeError")
    assert len(res["error"]) <= 200   # "Type: " 前缀 + 截断 150 字符
    st = json.load(open(status, encoding="utf-8"))
    assert st["archive"]["ok"] is False


def test_n6_record_status_merges(iso):
    """既有任务状态字段保留 + archive 段并入（读改写合并语义）。"""
    FIX.clear(); FIX.update(SIX)
    tmp_path, arch, status = iso
    fsutil.save_json_atomic(status, {"last_run": "L", "rounds": 3, "ok": True})
    auc_archive.archive("20260925", quiet=True)
    st = json.load(open(status, encoding="utf-8"))
    assert st["last_run"] == "L" and st["rounds"] == 3
    assert st["archive"]["ok"] is True and st["archive"]["date"] == "20260925"


def test_n7_record_status_survives_bad_status_file(iso):
    """状态文件是非 dict JSON（如半截被并发覆坏）：重置为 {} 不抛，归档照常成功。"""
    FIX.clear(); FIX.update(SIX)
    tmp_path, arch, status = iso
    with open(status, "w", encoding="utf-8") as f:
        json.dump([], f)
    res = auc_archive.archive("20260925", quiet=True)
    assert res["ok"] is True
    st = json.load(open(status, encoding="utf-8"))
    assert st["archive"]["ok"] is True


def test_n8_path_and_int_date(iso):
    FIX.clear(); FIX.update(SIX)
    assert auc_archive.archive_path("20260925").endswith(
        os.path.join("archive", "auc-20260925.json.gz"))
    res = auc_archive.archive(20260925, quiet=True)   # int 形态不炸（str 归一）
    assert res["ok"] is True and res["date"] == "20260925"


def test_n9_integration_real_data_copies(iso, monkeypatch):
    """集成：真实 data/auction 产物复制到 tmp 后完整归档-读回（不触碰生产文件）。"""
    src = C.DATA_DIR
    names = [n + ".json" for n in auc_archive.FILES]
    have = {n: os.path.exists(os.path.join(src, n)) for n in names}
    if not all(have.values()):
        pytest.skip(f"本机缺少部分真实竞价产物: {[n for n, ok in have.items() if not ok]}")
    tmp_path, arch, status = iso
    tmp_data = tmp_path / "data"
    os.makedirs(tmp_data)
    for n in names:
        with open(os.path.join(src, n), encoding="utf-8") as f:
            obj = json.load(f)
        fsutil.save_json_atomic(os.path.join(str(tmp_data), n), obj)
    monkeypatch.setattr(C, "load_json",
                        lambda name: json.load(open(os.path.join(str(tmp_data), name),
                                                    encoding="utf-8")))
    res = auc_archive.archive("20260924", quiet=True)
    assert res["ok"] is True and len(res["files"]) == 6
    back = json.load(gzip.open(res["path"]))
    assert back["v"] == 1 and back["date"] == "20260924"
    n_rounds = len(back["files"]["series"]["rounds"])
    assert n_rounds == len(back["files"]["rounds_meta"]["rounds"]) > 0


# ---------------------------------------------------------------- TC 组：F1/F2 修复行为

def test_tc1_save_status_preserves_archive_section(iso):
    """F2：save_status 整写覆盖其余键，但 archive 段跨运行保留（失败日/非交易日
    的短状态不得抹掉最近一次归档记录，否则 /api/health 归档可见性抖动）。"""
    tmp_path, arch, status = iso
    fsutil.save_json_atomic(status, {"last_run": "old", "ok": True,
                                     "archive": {"date": "20260924", "ok": True}})
    C.save_status({"last_run": "new", "ok": False, "note": "non-trade-day"})
    st = json.load(open(status, encoding="utf-8"))
    assert st["last_run"] == "new" and st["ok"] is False
    assert st["archive"] == {"date": "20260924", "ok": True}
    # 调用方显式携带 archive 时以其为准（当日新归档覆盖旧值）
    C.save_status({"last_run": "newer", "archive": {"date": "20260925", "ok": True}})
    st = json.load(open(status, encoding="utf-8"))
    assert st["archive"]["date"] == "20260925"


def test_tc2_save_status_survives_corrupt_file(iso):
    tmp_path, arch, status = iso
    with open(status, "w", encoding="utf-8") as f:
        f.write("{corrupt")
    C.save_status({"last_run": "fresh"})   # 不抛
    st = json.load(open(status, encoding="utf-8"))
    assert st == {"last_run": "fresh"}


def test_tc3_timeline_final_failure_still_archives(iso, monkeypatch):
    """F1：_timeline 终态抓取异常的早退路径必须归档——live 轮已在 series 里，
    不归档则次日 09:14 覆盖式落盘把当日采集永久带走。"""
    import auc_collector as AC
    tmp_path, arch, status = iso
    FIX.clear()
    FIX.update({"series.json": SIX["series"], "rounds_meta.json": SIX["rounds_meta"]})

    calls = []
    monkeypatch.setattr(AC, "is_trade_today", lambda: True)
    monkeypatch.setattr(C, "build_watchlist",
                        lambda: (["600000.SH"], {"total": 1, "user": 1, "auto": 0}))
    # 空窗口跳过 live 轮询；终态时刻放过去（不 sleep）；collect_round 抛 = 终态抓取失败
    monkeypatch.setattr(AC, "_today_at",
                        lambda hms: {C.LIVE_START: 1e12, C.LIVE_END: 0.0,
                                     C.FINAL_AT: -1e9}[hms])

    def boom(*a, **k):
        raise RuntimeError("cli down")

    monkeypatch.setattr(AC, "collect_round", boom)
    monkeypatch.setattr(auc_archive, "archive",
                        lambda d, **k: calls.append(d) or {"ok": True})
    AC._timeline("20260925")
    assert calls == ["20260925"]
    st = json.load(open(status, encoding="utf-8"))
    assert st["ok"] is False and "终态抓取异常" in st["note"]


def test_tc4_skip_paths_do_not_archive(iso, monkeypatch):
    """非交易日/空池早退路径不得归档：data/auction 里还是上一日内容，
    误归档会把昨日数据错标成今日。"""
    import auc_collector as AC
    tmp_path, arch, status = iso
    calls = []
    monkeypatch.setattr(auc_archive, "archive",
                        lambda d, **k: calls.append(d) or {"ok": True})
    monkeypatch.setattr(AC, "is_trade_today", lambda: False)
    AC._timeline("20260925")
    assert calls == []                      # 非交易日：不归档
    monkeypatch.setattr(AC, "is_trade_today", lambda: True)
    monkeypatch.setattr(C, "build_watchlist", lambda: ([], {"total": 0}))
    AC._timeline("20260925")
    assert calls == []                      # 空池：不归档
