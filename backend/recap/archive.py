# -*- coding: utf-8 -*-
"""快照归档：把 N 天前的明文快照压成 .json.gz（约 253KB -> ~20KB），原文件删除。

读取侧（server / derive / check / fetch）全部走 snapio，天然兼容两种形态。

用法:
  python backend/recap/archive.py            # 归档 14 天前的快照
  python backend/recap/archive.py --older-than 30 --dry-run
"""
import argparse
import datetime
import glob
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_BACKEND_ROOT = os.path.dirname(HERE)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
import snapio   # noqa: E402
import fsutil   # noqa: E402  原子写盘单一来源（backend/fsutil.py）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--older-than", type=int, default=14, help="归档 N 天前的快照（默认 14）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cutoff = (datetime.date.today() - datetime.timedelta(days=args.older_than)).strftime("%Y%m%d")
    total_before = total_after = 0
    n = 0
    for fp in sorted(glob.glob(os.path.join(snapio.RECAP_DATA, "*.json"))):
        name = os.path.basename(fp)
        date8 = name[:-5]
        if not (len(date8) == 8 and date8.isdigit()) or date8 >= cutoff:
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                data = f.read()
        except Exception as e:
            print(f"跳过 {name}: {e}")
            continue
        gz = fp + ".gz"
        before = len(data.encode("utf-8"))
        blob = gzip.compress(data.encode("utf-8"), compresslevel=9)   # 压一次复用
        after = len(blob)
        if not args.dry_run:
            fsutil.save_bytes_atomic(gz, blob)   # 原子写（半截 .gz 同样不可读）
            # 校验 gzip 可解析后再删原文件
            try:
                json.loads(gzip.open(gz, "rt", encoding="utf-8").read())
            except Exception as e:
                os.remove(gz)
                print(f"跳过 {name}: 压缩后校验失败 {e}")
                continue
            os.remove(fp)
            after = os.path.getsize(gz)
        n += 1
        total_before += before
        total_after += after
        print(f"{'[dry] ' if args.dry_run else ''}{name}: {before//1024}KB -> {after//1024}KB")
    if n:
        print(f"归档 {n} 份：{total_before/1048576:.1f}MB -> {total_after/1048576:.1f}MB")
    else:
        print("没有可归档的快照")


if __name__ == "__main__":
    main()
