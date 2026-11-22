# -*- coding: utf-8 -*-
"""全球热力图 A 股窗口「人工只读探针」——诊断 0/N 是限流、时点还是改版。

**不调度、不进 CI/门禁、不写 data/**（也不写 .status）；只在人手需要时运行。**
它复用生产同一套取数/判定（fetch_global.ht / _thscode / _chg / WIN5 与
window_status.summary），对当日 live 快照里按 ticker 排序的前 N 只个股逐只拉
5 日窗口，打印 status + n_ok/n_total + 失败代码的 JSON，便于和 `[warn] heatmap_cn`
日志对账。逐只异常只计失败、不中断探针。

用法（仓库根目录，Windows 示例）:
    .venv-ci/Scripts/python.exe tools/probe_global_cn.py --limit 50 --pause 0.3 --verbose

输出示例:
    {"summary": {"label": "cn", "status": "unknown", "n_ok": 0, "n_total": 50},
     "log_line": "heatmap_cn windows=0/50 status=unknown", "failed": ["000001", ...]}
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "backend"),
           os.path.join(ROOT, "backend", "global"),
           os.path.join(ROOT, "backend", "recap")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetch_global as fg  # noqa: E402
import window_status  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="global 热力图 A 股窗口只读探针（不调度、不进 CI/门禁、不写 data/**）")
    ap.add_argument("--limit", type=int, default=50, help="抽样只数（默认 50）")
    ap.add_argument("--pause", type=float, default=0.3, help="逐只间隔秒数（默认 0.3）")
    ap.add_argument("--verbose", action="store_true", help="逐只打印 ok/失败")
    args = ap.parse_args(argv)

    rows = fg.ht.market_snapshot_all()
    # 与 fetch_heat_cn 的 live 过滤同口径（fetch_global.py:581-583）
    live = [r for r in rows if r.get("last_price") and r.get("turnover")
            and r.get("price_change_ratio_pct") is not None]
    live = sorted(live, key=lambda r: str(r.get("ticker") or ""))[:max(0, args.limit)]
    print(f"[probe] 快照 {len(rows)} 行，live {len(live)} 只待探（limit={args.limit}）")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 60 * 86400 * 1000   # 与 _add_windows_cn 同窗口
    n_ok, failed = 0, []
    for r in live:
        code = str(r.get("ticker") or "")
        if args.pause > 0:
            time.sleep(args.pause)
        try:
            d = fg.ht.ht("market", "history", "--thscode", fg._thscode(code),
                         "--start-ms", str(start_ms), "--end-ms", str(end_ms),
                         timeout=40)
            items = sorted(d.get("item") or [], key=lambda x: x.get("date_ms") or 0)
            closes = [float(x["close_price"]) for x in items if x.get("close_price")]
            ok = fg._chg(closes, fg.WIN5) is not None
        except Exception as e:  # noqa: BLE001  探针逐只失败只计数，不中断
            ok = False
            if args.verbose:
                print(f"  [fail] {code} {type(e).__name__}: {str(e)[:100]}")
        if ok:
            n_ok += 1
        else:
            failed.append(code)
        if args.verbose:
            print(f"  [{'ok' if ok else 'fail'}] {code}")

    stat = window_status.summary("cn", n_ok, len(live))
    print(json.dumps({"summary": stat,
                      "log_line": window_status.log_line("heatmap_cn", stat),
                      "failed": failed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
