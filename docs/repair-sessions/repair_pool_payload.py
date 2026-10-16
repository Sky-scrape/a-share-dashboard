# -*- coding: utf-8 -*-
"""一次性修复：快照内嵌的「昨日备选池验证 / 优化器」载荷落后或破损。

背景（2026-09-20 实扫全 174 份快照后确定）：
  0914 事故的验证丢样在 2026-09-19 C12/C13 批次才用腾讯备源链路补算回
  pool_track.json（record_validation('20260914') 幂等覆盖写），但 0914~0917
  四份快照是 09-15~09-18 凌晨链写下的、早于该补算，内嵌副本仍是旧值：
    20260914 验证 3 票 close=None（前端显示「停牌/数据缺失」）、样本外 3日/3笔 severe
    20260915 样本外 4日/5笔 warn（应 8 笔 ok）
    20260916 样本外 5日/5笔 warn（应 8 笔 ok）
    20260917 样本外 6日/7笔 warn（应 10 笔 ok）
  另有 20260908 的验证/优化器整段是 NameError 破损载荷（当年代码 bug），
  以及 20260910/20260911 缺 oos.drift 字段（漂移功能 2026-09-16 才上线）。

不变式（修复后应全站成立）：
  snapshot.pool_validation == pool_track[date]
  snapshot.pool_optimizer  == update_optimizer(date, persist=False)

严格不做的事：
  - 不写 pool_track.json / pool_opt.json（账本已是权威，本脚本只读）；
  - 不动 pool.picks / pool.env / pool.note / fetched_at（选股结果与口径版本属
    快照冻结范围，改池口径不回填历史是项目纪律）；
  - 不动 20260903/20260904/20260907/20260909：这四份是 M_Final/C_Final 旧线
    快照，数据完整无缺失；当前 update_optimizer 只统计 rules=="c7" 样本，重算会
    把它们的旧线统计（0907 samples 2/1、0909 samples 4/1）清零，属数据破坏而非
    回填。0908 之所以可重算：它的载荷是破损的 NameError，本就无真实数据可丢。

用法：
    python .status/repair_pool_payload.py --dry-run   # 只打印差异
    python .status/repair_pool_payload.py             # 实际写回
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))

import fsutil        # noqa: E402  原子写盘单一来源
import snapio        # noqa: E402  快照路径解析单一来源
import spec_validate as sv  # noqa: E402  验证/优化器纯函数单一来源

TARGETS = ["20260908", "20260910", "20260911",
           "20260914", "20260915", "20260916", "20260917"]


def _recompute_optimizer(track, date8):
    """按「截至 date8 的账本」重算优化器载荷（persist=False，绝不落账）。

    update_optimizer 内部自读 POOL_TRACK，故把该常量临时指向截断账本的临时文件，
    语义 = 当日快照写下时应当看到的纯函数结果。"""
    truncated = {"validations": {k: v for k, v in (track.get("validations") or {}).items()
                                 if k <= date8}}
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    orig = sv.POOL_TRACK
    try:
        fsutil.save_json_atomic(tmp, truncated)
        sv.POOL_TRACK = tmp
        return sv.update_optimizer(date8, persist=False)
    finally:
        sv.POOL_TRACK = orig
        try:
            os.remove(tmp)
        except OSError:
            pass


def _diff_keys(a, b, path=""):
    out = []
    if type(a) is not type(b):
        return [f"{path}: 类型 {type(a).__name__} -> {type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: 缺失 -> 新增")
            elif k not in b:
                out.append(f"{path}.{k}: 存在 -> 移除")
            else:
                out += _diff_keys(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: 长度 {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff_keys(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")
    return out


def main():
    ap = argparse.ArgumentParser(description="回填快照内嵌的验证/优化器载荷")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写盘")
    args = ap.parse_args()

    track = sv._load_track()
    vals = track.get("validations") or {}
    n_written = 0
    for date8 in TARGETS:
        if date8 not in vals:
            print(f"  {date8}: 账本无该验证日，跳过")
            continue
        path = snapio.snap_path(snapio.RECAP_DATA, date8)
        snap = snapio.load(date8)
        if snap is None:
            print(f"  {date8}: 快照缺失/损坏，跳过")
            continue
        snap = json.loads(json.dumps(snap))   # 脱手共享缓存，改写前深拷贝
        sp = ((snap.get("modules") or {}).get("speculation") or {}).get("data")
        if not isinstance(sp, dict):
            print(f"  {date8}: 无 speculation.data，跳过")
            continue

        new_val = vals[date8]
        new_opt = _recompute_optimizer(track, date8)
        d_val = _diff_keys(sp.get("pool_validation") or {}, new_val, "pool_validation")
        d_opt = _diff_keys(sp.get("pool_optimizer") or {}, new_opt, "pool_optimizer")
        print(f"== {date8}: pool_validation {len(d_val)} 处 / pool_optimizer {len(d_opt)} 处差异")
        for x in (d_val + d_opt)[:8]:
            print("   ", x)
        if len(d_val) + len(d_opt) > 8:
            print(f"    ...（共 {len(d_val) + len(d_opt)} 处）")
        if not d_val and not d_opt:
            print("    已是最新，无需写回")
            continue

        sp["pool_validation"] = new_val
        sp["pool_optimizer"] = new_opt
        pool = sp.get("pool")
        if isinstance(pool, dict):
            if pool.get("validation") is not None:
                pool["validation"] = new_val
            if pool.get("optimizer") is not None:
                pool["optimizer"] = new_opt
        if args.dry_run:
            print("    [dry-run] 未写盘")
            continue
        snapio.save(date8, snap)
        n_written += 1
        print(f"    已写回 {os.path.basename(path)}")
    print(f"== 完成：写回 {n_written} 份" + ("（dry-run）" if args.dry_run else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
