# -*- coding: utf-8 -*-
"""数据资产备份（2026-09-15）· data/ 与策略研究库滚动打包。

背景：data/（复盘冻结快照、轮动分时、竞价采集）与 strategy-iter/{data,raw}
（本地研究库导出、概念/行业/池子原始抓取）全部在 .gitignore 里，没有任何
备份；复盘冻结快照受「不回填」纪律保护、分时数据不可回补、研究库逐日积累
——上游竞态（0914 事故）刚证明这些资产丢一次就是真丢。本脚本把它们打成
单 zip 滚动保留（默认 10 份），由计划任务 arecap-backup 每日 12:10 调用。

默认排除可再生派生层（--full 可包含）：data/cache/、data/recap/panel/、
data/rotation/panel/——它们由 derive.py 从快照重建，占了备份体积的一半以上。

状态落 .status/backup_state.json（health backup 段/健康面板消费；配置在
.status/backup.json 可改备份目录与保留份数）；失败退出码 1（计划任务日志
可见），本身不推告警——由 arecap-watchdog 的 health 巡检发现 backup.ok=false
后统一推送，避免两条链路重复打扰。
"""
import argparse
import datetime as _dt
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATUS_DIR = os.path.join(ROOT, ".status")
CONFIG_PATH = os.path.join(STATUS_DIR, "backup.json")
STATUS_PATH = os.path.join(STATUS_DIR, "backup_state.json")
DEFAULT_KEEP = 10

# 备份源（相对 ROOT）：快照/分时/竞价/研究库等不可再生资产
SOURCES = ["data", os.path.join("strategy-iter", "data"), os.path.join("strategy-iter", "raw")]
# 可再生派生层（默认排除；derive.py 可从快照重建）
DERIVED = ["data/cache/", "data/recap/panel/", "data/rotation/panel/"]
# 通用排除：缓存与锁文件
SKIP_NAMES = {"__pycache__", ".git"}
SKIP_EXT = (".lock", ".pyc", ".tmp")


def load_config():
    """备份配置（.status/backup.json：dest/keep）；缺失/损坏用默认。"""
    base = {"dir": os.path.join(os.path.expanduser("~"), "AreCapBackups"),
            "keep": DEFAULT_KEEP}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            if d.get("dir"):
                base["dir"] = str(d["dir"])
            if d.get("keep"):
                base["keep"] = int(d["keep"])
    except Exception:  # noqa: BLE001 - 无配置文件=默认口径
        pass
    return base


def _status_write(d):
    try:
        sys.path.insert(0, os.path.dirname(HERE))
        from fsutil import save_json_atomic
        os.makedirs(STATUS_DIR, exist_ok=True)
        save_json_atomic(STATUS_PATH, d)
    except Exception:  # noqa: BLE001 - 状态写失败不影响备份本身
        pass


def _wanted(rel_path, full):
    """rel_path 形如 data\\recap\\20260915.json；返回是否纳入备份。"""
    p = rel_path.replace(os.sep, "/") + ("/" if rel_path.endswith(os.sep) else "")
    if not full and any(p.startswith(d) for d in DERIVED):
        return False
    name = os.path.basename(rel_path)
    if name in SKIP_NAMES or rel_path.split(os.sep)[0] in SKIP_NAMES:
        return False
    return not name.endswith(SKIP_EXT)


def run(full=False, dest_override=None, keep_override=None):
    cfg = load_config()
    dest = dest_override or cfg["dir"]
    keep = keep_override or cfg["keep"]
    t0 = time.time()
    stamp = time.strftime("%Y%m%d-%H%M")
    os.makedirs(dest, exist_ok=True)
    zpath = os.path.join(dest, f"arecap-data-{stamp}.zip")
    tmp = zpath + ".tmp"
    n_files = 0
    skipped = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for src in SOURCES:
                src_abs = os.path.join(ROOT, src)
                if not os.path.isdir(src_abs):
                    continue
                for dirpath, dirnames, filenames in os.walk(src_abs):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
                    for fn in filenames:
                        abs_p = os.path.join(dirpath, fn)
                        rel = os.path.relpath(abs_p, ROOT)
                        if not _wanted(rel, full):
                            skipped += 1
                            continue
                        try:
                            z.write(abs_p, rel.replace(os.sep, "/"))
                            n_files += 1
                        except OSError:
                            skipped += 1
            z.writestr("_backup_manifest.json", json.dumps({
                "created": _dt.datetime.now().isoformat(timespec="seconds"),
                "sources": SOURCES,
                "excluded_derived": None if full else DERIVED,
                "files": n_files}, ensure_ascii=False), compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp, zpath)   # 同盘原子替换：半截 zip 不会顶掉旧备份
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        _status_write({"ok": False, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "error": f"{type(e).__name__}: {str(e)[:200]}",
                       "dest": dest, "keep": keep})
        raise

    # 滚动保留：只删本脚本命名规则的旧包，绝不碰目录里的其他文件
    mine = sorted(f for f in os.listdir(dest)
                  if f.startswith("arecap-data-") and f.endswith(".zip"))
    removed = []
    for old in mine[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(dest, old))
            removed.append(old)
        except OSError:
            pass
    size_mb = round(os.path.getsize(zpath) / 1e6, 1)
    dur = round(time.time() - t0, 1)
    _status_write({"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "duration_s": dur, "size_mb": size_mb, "files": n_files,
                   "skipped_derived": skipped, "dest": dest, "keep": keep,
                   "kept": len(mine) - len(removed), "removed": removed[-3:],
                   "last_zip": os.path.basename(zpath),
                   "same_drive_as_project": os.path.splitdrive(dest)[0].upper()
                   == os.path.splitdrive(ROOT)[0].upper(),
                   "error": None})
    return {"zip": zpath, "size_mb": size_mb, "files": n_files, "dur": dur,
            "kept": len(mine) - len(removed), "dest": dest}


def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="数据资产滚动备份（data/ + strategy-iter 研究库）")
    ap.add_argument("--full", action="store_true", help="包含可再生派生面板（默认排除）")
    ap.add_argument("--dest", help="覆盖配置的备份目录")
    ap.add_argument("--keep", type=int, help="覆盖配置的保留份数")
    args = ap.parse_args()
    r = run(full=args.full, dest_override=args.dest, keep_override=args.keep)
    print(f"备份完成: {r['zip']}  {r['size_mb']}MB / {r['files']} 文件 / {r['dur']}s "
          f"（保留 {r['kept']} 份 → {r['dest']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
