"""涨停情绪面板：经 hithink special-data 拉涨停/炸板/跌停池，逐日缓存出趋势。

口径（写死并在 UI 标注，防自欺）：
* 炸板率 = 炸板数 / (收盘涨停数 + 炸板数)（盘中触板未封住近似）；
* 晋级率 = 今日连板≥2 家数 / 昨日涨停总数（只算"续命"，不含新进首板分化）；
* 连板高度 = 今日涨停池 continue_day_cnt 最大值；
* 数据为收盘后口径；非交易日 CLI 返回 total=0，本模块只查日历内交易日。

缓存：``data/sentiment/raw_YYYYMMDD.json``（每交易日一份原始池摘要）。
CLI 入口：``python -m quant_sim.research.sentiment --days 20`` 增量补齐，
供定时任务保持缓存新鲜。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from quant_sim.data.hithink import _cli

CACHE_DIR = (Path(__file__).resolve().parents[2] / "data" / "sentiment")

__all__ = ["fetch_day", "load_cached_day", "sentiment_frame", "update_cache"]


def _date_ms(d: pd.Timestamp) -> int:
    return int(pd.Timestamp(d).tz_localize("Asia/Shanghai").timestamp() * 1000)


def _pool_all(cmd: str, date_ms: int, size: int = 200, max_pages: int = 3) -> tuple[List[dict], int]:
    """翻完一页 size=200（涨停池日常 <150 家），返回 (items, total)。"""
    items: List[dict] = []
    total = 0
    for page in range(1, max_pages + 1):
        data = _cli(["special", cmd, "--date-ms", str(date_ms), "--page", str(page), "--size", str(size)])
        total = int((data.get("pagination") or {}).get("total", 0))
        items.extend(data.get("item") or [])
        if len(items) >= total or not (data.get("item") or []):
            break
    return items, total


def fetch_day(date: pd.Timestamp) -> dict:
    """一个交易日的情绪原始数据（缓存 JSON 的载荷）。"""
    up, up_total = _pool_all("limit-up-pool", _date_ms(date))
    # 炸板/跌停只要家数：size=1 读 total
    bd = _cli(["special", "limit-break-pool", "--date-ms", str(_date_ms(date)), "--page", "1", "--size", "1"])
    dn = _cli(["special", "limit-down-pool", "--date-ms", str(_date_ms(date)), "--page", "1", "--size", "1"])
    ladder: Dict[int, int] = {}
    for r in up:
        c = int(r.get("continue_day_cnt") or 1)
        ladder[c] = ladder.get(c, 0) + 1
    top = sorted(up, key=lambda r: -(r.get("seal_money") or 0))[:8]
    return {
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "n_up": up_total,
        "n_break": int((bd.get("pagination") or {}).get("total", 0)),
        "n_down": int((dn.get("pagination") or {}).get("total", 0)),
        "ladder": {str(k): v for k, v in sorted(ladder.items(), reverse=True)},
        "max_lianban": max((int(r.get("continue_day_cnt") or 1) for r in up), default=0),
        "n_lianban_2plus": sum(1 for r in up if int(r.get("continue_day_cnt") or 1) >= 2),
        "st_up": sum(1 for r in up if r.get("is_st")),
        "top": [
            {
                "name": r.get("name"),
                "lianban": int(r.get("continue_day_cnt") or 1),
                "seal_yi": round((r.get("seal_money") or 0) / 1e8, 2),
                "reason": r.get("limit_up_reason") or "",
                "time": r.get("limit_up_time") or "",
            }
            for r in top
        ],
    }


def cache_path(date: pd.Timestamp) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"raw_{pd.Timestamp(date).strftime('%Y%m%d')}.json"


def load_cached_day(date: pd.Timestamp) -> Optional[dict]:
    p = cache_path(date)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def update_cache(dates: Sequence[pd.Timestamp], progress=None) -> dict:
    """补齐缺失交易日的缓存；返回 {fetched, cached, failures}。"""
    fetched = cached = 0
    failures: List[str] = []
    for d in dates:
        if load_cached_day(d) is not None:
            cached += 1
            continue
        try:
            payload = fetch_day(d)
            cache_path(d).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            fetched += 1
            if progress:
                progress(f"{payload['date']}：涨停 {payload['n_up']} 家")
        except Exception as e:
            failures.append(f"{pd.Timestamp(d).date()}: {e}")
    return {"fetched": fetched, "cached": cached, "failures": failures}


def sentiment_frame(dates: Sequence[pd.Timestamp]) -> pd.DataFrame:
    """从缓存拼出日频情绪表（缺缓存的日子为 NaN，不在这里触发抓取）。"""
    rows = []
    for i, d in enumerate(dates):
        raw = load_cached_day(d)
        if raw is None:
            continue
        prev = load_cached_day(dates[i - 1]) if i > 0 else None
        n_up_prev = prev["n_up"] if prev else None
        row = {
            "date": pd.Timestamp(d),
            "涨停": raw["n_up"],
            "跌停": raw["n_down"],
            "炸板": raw["n_break"],
            "炸板率": round(raw["n_break"] / max(raw["n_up"] + raw["n_break"], 1), 3),
            "连板高度": raw["max_lianban"],
            "连板家数(≥2)": raw["n_lianban_2plus"],
            "晋级率": (round(raw["n_lianban_2plus"] / n_up_prev, 3) if n_up_prev else None),
            "ST涨停": raw["st_up"],
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.set_index("date") if not df.empty else df


def latest_raw(dates: Sequence[pd.Timestamp]) -> Optional[dict]:
    for d in reversed(list(dates)):
        raw = load_cached_day(d)
        if raw is not None:
            return raw
    return None


def _main() -> None:
    ap = argparse.ArgumentParser(description="补齐涨停情绪缓存（供定时任务/手动预热）")
    ap.add_argument("--days", type=int, default=20, help="最近 N 个交易日")
    args = ap.parse_args()
    # 交易日历用本地个股库（与行情桥接同源）：返回的是日期集合
    from quant_sim.data.hithink import _cn_trading_dates

    today = pd.Timestamp.today().normalize()
    all_days = sorted(pd.Timestamp(d) for d in _cn_trading_dates() if pd.Timestamp(d) <= today)
    days = all_days[-args.days:]
    if not days:
        print("本地交易日历为空，先跑 hithink-finance data sync")
        return
    stat = update_cache(days, progress=lambda s: print(s))
    print(f"情绪缓存：新抓 {stat['fetched']}，命中 {stat['cached']}，失败 {len(stat['failures'])}")
    for f in stat["failures"][:5]:
        print("  !", f)


if __name__ == "__main__":
    _main()
