# -*- coding: utf-8 -*-
"""全球总览抓取：产出 data/global/global.json。

三大块（全部可部分降级，单块失败不连坐）：
1. indices   世界主要国家/地区指数：最新收盘、日/5日/20日涨跌 + 约250日历史（地图着色 + 点击走势）。
   数据源：新浪（akshare sina 通道：index_global_hist_sina / index_us_stock_sina /
   stock_zh_index_daily / stock_hk_index_daily_sina）。东财通道本机网络不通，勿用。
2. radar     轮动雷达（美股 vs A股，9 大主题 × 5/20 日动量）。
   美股侧：SPDR 行业 ETF（stock_us_daily）；A股侧：本地复盘快照 boards 逐日涨跌幅复合。
3. heatmap   热力图。美股侧：新浪 gb_ 实时批量（约50只巨头，市值定大小、涨跌幅着色）；
   A股侧：hithink 全市场快照 + 行业映射聚合（行业成交额定大小、加权涨跌幅着色，含各行业成交前列个股）。

运行：python backend/global/fetch_global.py   （建议每日 18:00 后，美股/欧股收盘齐之后）
"""
import io
import json
import os
import random
import sys
import time

# 禁用系统代理：直连新浪/东财接口（坏代理会导致请求挂起，与 server/providers 同一约定）
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
from pathlib import Path  # noqa: E402

import lockutil  # noqa: E402
import logutil  # noqa: E402  统一 logging（时间戳/级别）
import http_retry  # noqa: E402  共享重试（backend/http_retry.py）

LOG = logutil.get_logger("ak.global")
import ht  # noqa: E402  hithink CLI 封装（A股热力图）
import snapio  # noqa: E402 复盘快照读取（A股雷达动量）
import thscodes  # noqa: E402  代码转换单一来源（backend/thscodes.py）

RECAP_DATA = os.path.join(ROOT, "data", "recap")
HT_CACHE = os.path.join(ROOT, "backend", "recap", ".ht_cache")
OUT_DIR = os.path.join(ROOT, "data", "global")
OUT = os.path.join(OUT_DIR, "global.json")
LOCK_PATH = os.path.join(ROOT, ".status", "fetch-global.lock")

HIST_N = 250          # 每个指数保留的日点数（点击走势用）
RETRY = 2             # 网络类失败重试次数


def _retry(fn, tries=RETRY, delay=2):
    """网络类失败重试（实现在 backend/http_retry.py，与 providers 同一实现）。"""
    return http_retry.retry(fn, tries=tries, delay=delay)


# ---------------- 1. 环球指数 ----------------

# id, 中文名, 国别(地图着色), 城市, tz, lon, lat, geo(世界地图英文名), 取数器
def _hist_cn(sym):
    return _retry(lambda: __import__("akshare").stock_zh_index_daily(symbol=sym))


def _hist_us(sym):
    return _retry(lambda: __import__("akshare").index_us_stock_sina(symbol=sym))


def _hist_sina_global(sym):
    import akshare as ak
    return _retry(lambda: ak.index_global_hist_sina(symbol=sym))


def _hist_hk(sym):
    import akshare as ak
    return _retry(lambda: ak.stock_hk_index_daily_sina(symbol=sym))


INDEX_SPECS = [
    ("SHA",    "上证指数",   "中国", "上海", "Asia/Shanghai",      121.47, 31.23,  "China",   lambda: _hist_cn("sh000001")),
    ("SZA",    "深证成指",   "中国", "深圳", "Asia/Shanghai",      114.30, 22.80,  "China",   lambda: _hist_cn("sz399001")),
    ("HSI",    "恒生指数",   "中国", "香港", "Asia/Hong_Kong",     114.17, 22.32,  "China",   lambda: _hist_hk("HSI")),
    ("SPX",    "标普500",    "美国", "纽约", "America/New_York",   -74.01, 40.71,  "United States", lambda: _hist_us(".INX")),
    ("IXIC",   "纳斯达克",   "美国", "纽约", "America/New_York",   -73.85, 40.90,  "United States", lambda: _hist_us(".IXIC")),
    ("UKX",    "富时100",    "英国", "伦敦", "Europe/London",      -0.12,  51.51,  "United Kingdom", lambda: _hist_sina_global("英国富时100指数")),
    ("DAX",    "德国DAX",    "德国", "法兰克福", "Europe/Berlin",    8.68,   50.11,  "Germany", lambda: _hist_sina_global("德国DAX 30种股价指数")),
    ("CAC",    "法国CAC40",  "法国", "巴黎", "Europe/Paris",       2.35,   48.86,  "France",  lambda: _hist_sina_global("法CAC40指数")),
    ("NKY",    "日经225",    "日本", "东京", "Asia/Tokyo",         139.69, 35.68,  "Japan",   lambda: _hist_sina_global("日经225指数")),
    ("KOSPI",  "韩国综合",   "韩国", "首尔", "Asia/Seoul",         126.98, 37.57,  "Korea",   lambda: _hist_sina_global("首尔综合指数")),
    ("SENSEX", "印度SENSEX", "印度", "孟买", "Asia/Kolkata",       72.83,  19.08,  "India",   lambda: _hist_sina_global("印度孟买SENSEX指数")),
    ("AS51",   "澳标普200",  "澳大利亚", "悉尼", "Australia/Sydney", 151.21, -33.87, "Australia", lambda: _hist_sina_global("澳大利亚标准普尔200指数")),
    ("GSPTSE", "加TSX",      "加拿大", "多伦多", "America/Toronto",  -79.38, 43.65,  "Canada",  lambda: _hist_sina_global("加拿大S&P/TSX综合指数")),
    ("IBOV",   "巴西BOVESPA", "巴西", "圣保罗", "America/Sao_Paulo",  -46.63, -23.55, "Brazil",  lambda: _hist_sina_global("巴西BOVESPA股票指数")),
    ("FTSEMIB", "意大利MIB",  "意大利", "米兰", "Europe/Rome",      12.50,  41.90,  "Italy",   lambda: _hist_sina_global("富时意大利MIB指数")),
]


def _chg(closes, n):
    if len(closes) > n and closes[-1 - n]:
        return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)
    return None


WIN5, WIN20 = 5, 20   # 热力图周期窗口（交易日）


def fetch_indices():
    import akshare as ak  # noqa: F401  延迟导入由取数器内部完成
    out, errs = [], {}
    for (idx_id, name, country, city, tz, lon, lat, geo, fetch) in INDEX_SPECS:
        try:
            df = fetch()
            df = df.dropna(subset=["close"])
            # 新浪源会混入 0/负值占位行（如 KOSPI 尾行、SENSEX 零散节假日），
            # 不过滤会导致涨跌幅算成 -100%、走势图出现坠崖毛刺
            df = df[df["close"].astype(float) > 0]
            df = df.tail(HIST_N)
            rows = [[str(r["date"]), round(float(r["close"]), 2)]
                    for _, r in df.iterrows()]
            # 日线 OHLC（K 线用）：[date, open, close, low, high]，缺列/非法值用 close 兑底
            kl = []
            for _, r in df.iterrows():
                c = round(float(r["close"]), 2)
                try:
                    o = round(float(r.get("open") or 0), 2) or c
                    lo = round(float(r.get("low") or 0), 2) or c
                    hi = round(float(r.get("high") or 0), 2) or c
                except (TypeError, ValueError):
                    o = lo = hi = c
                kl.append([str(r["date"]), o, c, lo, hi])
            if len(rows) < 10:
                raise RuntimeError("历史数据过少")
            closes = [c for _, c in rows]
            out.append({
                "id": idx_id, "name": name, "country": country, "city": city,
                "tz": tz, "lon": lon, "lat": lat, "geo": geo,
                "close": closes[-1], "last_date": rows[-1][0],
                "pct": _chg(closes, 1), "chg5": _chg(closes, 5), "chg20": _chg(closes, 20),
                "hist": rows, "kline": kl,
            })
            LOG.info(f"[indices] {idx_id} {name}: {len(rows)} 点 @ {rows[-1][0]}")
        except Exception as e:  # noqa: BLE001
            errs[idx_id] = f"{type(e).__name__}: {str(e)[:120]}"
            LOG.info(f"[indices] {idx_id} FAIL {errs[idx_id]}")
    return out, errs


# ---------------- 2. 轮动雷达（RS × MOM × 近4周轨迹） ----------------

RADAR_AXES = ["科技", "传媒互联网", "金融", "医药", "消费", "高端制造", "新能源", "能源材料", "公用地产"]
MOM_WIN = 20      # 动量窗口：20 个交易日
TRAIL_WEEKS = 4   # 轨迹：近 4 周，每 5 个交易日取一个点（共 5 点）
TRAIL_STEP = 5

# 美股侧：SPDR 主题 ETF（列表即成员，组合主题取等权合成指数）
US_ETF_MAP = {
    "科技": ["XLK"], "传媒互联网": ["XLC"], "金融": ["XLF"], "医药": ["XLV"],
    "消费": ["XLY", "XLP"], "高端制造": ["XLI"], "新能源": ["TAN"],
    "能源材料": ["XLE", "XLB"], "公用地产": ["XLRE", "XLU"],
}

# A股侧：同花顺一级行业 → 主题（未列出的行业不参与）
CN_THEME_MAP = {
    "科技": ["半导体", "元件", "光学光电子", "消费电子", "电子化学品", "其他电子",
             "软件开发", "IT服务", "计算机设备", "通信设备"],
    "传媒互联网": ["文化传媒", "影视院线", "游戏"],
    "金融": ["银行", "证券", "保险", "多元金融"],
    "医药": ["化学制药", "中药", "生物制品", "医疗器械", "医疗服务", "医药商业"],
    "消费": ["白酒", "饮料制造", "食品加工制造", "种植业与林业", "养殖业", "农产品加工",
             "零售", "贸易", "服装家纺", "纺织制造", "美容护理", "旅游及酒店", "教育",
             "其他社会服务", "家居用品", "白色家电", "黑色家电", "小家电", "厨卫电器",
             "汽车整车", "汽车零部件", "汽车服务及其他", "互联网电商"],
    "高端制造": ["专用设备", "通用设备", "工程机械", "自动化设备", "电机", "轨交设备",
                 "军工装备", "军工电子", "建筑装饰"],
    "新能源": ["电池", "光伏设备", "风电设备", "其他电源设备", "电网设备", "能源金属"],
    "能源材料": ["煤炭开采加工", "油气开采及服务", "石油加工贸易", "化学原料", "化学制品",
                 "化学纤维", "钢铁", "工业金属", "贵金属", "小金属", "金属新材料",
                 "非金属材料", "建筑材料", "农化制品", "环境治理", "环保设备",
                 "橡胶制品", "塑料制品", "造纸", "包装印刷"],
    "公用地产": ["房地产", "电力", "燃气", "港口航运", "物流", "公路铁路运输", "机场航运"],
}

_BENCH_NAME = {"SPX": "标普500", "SHA": "上证指数"}


def _theme_point_metrics(series, bench, name, members):
    """series: [(date, level)] 主题等权合成净值；bench: 基准指数 hist [[d,c],...]。
    返回 {name,members,today,mom,rs,trail:[{d,rs,mom} 旧→新]}。"""
    import bisect
    dates = [d for d, _ in series]
    p = [v for _, v in series]
    L = len(p)
    bd = [d for d, _ in bench["hist"]]
    bc = [c for _, c in bench["hist"]]

    def bench_mom_at(date):
        i = bisect.bisect_right(bd, date) - 1
        if i < MOM_WIN:
            return None
        return (bc[i] / bc[i - MOM_WIN] - 1) * 100

    trail = []
    for k in range(TRAIL_WEEKS, -1, -1):
        pos = L - 1 - k * TRAIL_STEP
        if pos - MOM_WIN < 0:
            continue
        d = dates[pos]
        m = (p[pos] / p[pos - MOM_WIN] - 1) * 100
        bm = bench_mom_at(d)
        trail.append({"d": d, "mom": round(m, 2),
                      "rs": round(m - bm, 2) if bm is not None else None})
    if not trail:
        raise RuntimeError("主题序列过短")
    last = trail[-1]
    today = (p[-1] / p[-2] - 1) * 100 if L >= 2 else None
    return {"name": name, "members": members, "asof": dates[-1],
            "today": round(today, 2) if today is not None else None,
            "mom": last["mom"], "rs": last["rs"], "trail": trail}


def _us_theme_series(ak, etfs):
    """多 ETF 共同日期轴上的等权净值序列（首日=100）。"""
    maps = {}
    for sym in etfs:
        df = _retry(lambda sym=sym: ak.stock_us_daily(symbol=sym))
        df = df.dropna(subset=["close"]).tail(MOM_WIN + TRAIL_WEEKS * TRAIL_STEP + 12)
        maps[sym] = {str(r["date"])[:10]: float(r["close"])
                     for _, r in df.iterrows() if r["close"]}
    common = sorted(set.intersection(*[set(m) for m in maps.values()]))
    if len(common) < MOM_WIN + TRAIL_WEEKS * TRAIL_STEP + 2:
        raise RuntimeError("ETF 共同交易日不足")
    first = {s: maps[s][common[0]] for s in maps}
    return [(d, sum(maps[s][d] / first[s] for s in maps) * 100.0 / len(maps))
            for d in common]


def _load_board_daily(need):
    """从复盘快照读行业逐日涨跌幅 {board: {date: pct}}（同花顺一级行业）。"""
    dates = sorted(snapio.list_dates(RECAP_DATA))
    use_dates = dates[-need:]
    board_daily = {}   # board -> {date: pct}
    for d8 in use_dates:
        snap = snapio.load(d8, RECAP_DATA)
        if not snap:
            continue
        ds = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        boards = (snap.get("modules") or {}).get("boards") or {}
        for b in (boards.get("data") or []):
            nm, pct = b.get("名称"), b.get("涨跌幅")
            if nm and pct is not None:
                board_daily.setdefault(nm, {})[ds] = float(pct)
    if not board_daily:
        raise RuntimeError("复盘快照 boards 历史为空")
    return board_daily


def _cn_theme_daily():
    """合成主题日收益。返回 (theme_daily: {theme: [(date, day_pct)]}, n_dates)。"""
    need = MOM_WIN + TRAIL_WEEKS * TRAIL_STEP + 6
    board_daily = _load_board_daily(need)
    axis = sorted(set.intersection(*[set(v) for v in
                   (board_daily[n] for ns in CN_THEME_MAP.values() for n in ns if n in board_daily)]))
    theme_daily = {}
    for theme, names in CN_THEME_MAP.items():
        rows = []
        for d in axis:
            vals = [board_daily[n][d] for n in names if n in board_daily and d in board_daily[n]]
            if vals:
                rows.append((d, sum(vals) / len(vals)))
        theme_daily[theme] = rows
    return theme_daily, len(axis)


def fetch_radar(indices):
    """RS = 主题20日动量 − 基准20日动量（%），MOM = 主题20日涨幅（%）。
    基准：美股=标普500，A股=上证指数（复用本轮已抓的指数 hist）。"""
    import akshare as ak
    errs = {}
    bench = {x["id"]: x for x in indices}
    out = {"mom_win": MOM_WIN, "trail_weeks": TRAIL_WEEKS,
           "axes_order": RADAR_AXES}

    us_themes = []
    for theme, etfs in US_ETF_MAP.items():
        try:
            if "SPX" not in bench:
                raise RuntimeError("缺标普500基准")
            series = _us_theme_series(ak, etfs)
            us_themes.append(_theme_point_metrics(series, bench["SPX"], theme, etfs))
        except Exception as e:  # noqa: BLE001
            errs[f"us_{theme}"] = f"{type(e).__name__}: {str(e)[:100]}"
    out["us"] = {"benchmark": {"id": "SPX", "name": _BENCH_NAME["SPX"]},
                 "themes": us_themes}

    try:
        if "SHA" not in bench:
            raise RuntimeError("缺上证指数基准")
        theme_daily, n_axis = _cn_theme_daily()
        cn_themes = []
        for theme in RADAR_AXES:
            rows = theme_daily.get(theme) or []
            if len(rows) < MOM_WIN + TRAIL_WEEKS * TRAIL_STEP + 2:
                errs[f"cn_{theme}"] = "快照日数不足"
                continue
            p, acc = [], 100.0
            for d, daypct in rows:
                acc *= (1 + daypct / 100.0)
                p.append((d, acc))
            try:
                cn_themes.append(_theme_point_metrics(p, bench["SHA"], theme,
                                                      CN_THEME_MAP[theme]))
            except Exception as e:  # noqa: BLE001
                errs[f"cn_{theme}"] = f"{type(e).__name__}: {str(e)[:100]}"
        out["cn"] = {"benchmark": {"id": "SHA", "name": _BENCH_NAME["SHA"]},
                     "themes": cn_themes}
        out["meta"] = {"snapshot_axis": n_axis}
    except Exception as e:  # noqa: BLE001
        errs["cn"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["cn"] = None

    if not us_themes and not out.get("cn"):
        return None, errs
    return out, errs


# ---------------- 3. 热力图 ----------------

US_HEAT_SECTORS = {
    "科技": ["aapl", "msft", "nvda", "googl", "meta", "avgo", "orcl", "crm", "amd", "intc", "txn", "qcom"],
    "传媒互联网": ["nflx", "dis", "ea", "cmcsa", "vz", "tmus"],
    "金融": ["jpm", "bac", "wfc", "ms", "gs", "blk", "axp", "c", "v", "ma", "schw"],
    "医药": ["lly", "jnj", "abbv", "mrk", "pfe", "tmo", "abt", "amgn", "unh", "isrg"],
    "消费": ["wmt", "cost", "pg", "ko", "pep", "mcd", "nke", "sbux", "hd", "tgt"],
    "高端制造": ["ba", "cat", "ge", "hon", "ups", "fdx", "de", "etn", "mmm", "emr"],
    "新能源": ["tsla", "enph", "fslr", "plug"],
    "能源材料": ["xom", "cvx", "cop", "slb", "eog", "fcx", "nem", "dow", "dd", "lin"],
    "公用地产": ["pld", "amt", "cci", "nee", "duk", "d", "so", "o"],
}


def _us_daily_closes(sym, n=WIN20 + 3):
    """新浪美股日线不复权收盘（最近 n 条）。
    绕开 ak.stock_us_daily：它拉复权因子用小写 symbol，个股普遍 404 → eval(404页) SyntaxError。"""
    import pandas as pd
    import py_mini_racer
    from akshare.stock.stock_us_sina import zh_js_decode
    res = requests.get(f"https://finance.sina.com.cn/staticdata/us/{sym}", timeout=20)
    payload = res.text.split("=", 1)[1].split(";")[0].replace('"', "")
    js = py_mini_racer.MiniRacer()
    js.eval(zh_js_decode)
    df = pd.DataFrame(js.call("d", payload))
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    closes = [float(c) for c in df["close"] if c and float(c) > 0]
    # 窗口内若有真实拆分/除权（factor≠1 或 adjust≠0），不复权口径失真 → 降级 None
    try:
        rj = requests.get(
            f"https://finance.sina.com.cn/us_stock/company/reinstatement/{sym.upper()}_qfq.js",
            timeout=15)
        if rj.status_code == 200:
            data = (json.loads(rj.text.split("=", 1)[1].split(";")[0]) or {}).get("data") or []
            cut = str((pd.Timestamp.now() - pd.Timedelta(days=45)).date())
            real = [e for e in data if str(e.get("d", "")) >= cut
                    and (str(e.get("f")) not in ("1", "1.0") or str(e.get("c")) not in ("0", "0.0"))]
            if real:
                return None
    except Exception:  # noqa: BLE001  因子文件缺失不阻断，用不复权价
        pass
    return closes[-n:]


def _add_windows_us(sectors):
    """每只美股补 5/20 日涨幅；失败个股置 None，前端灰色降级。"""
    from concurrent.futures import ThreadPoolExecutor

    def one(x):
        x["pct5"] = x["pct20"] = None
        try:
            closes = _us_daily_closes(x["code"].lower())
            if closes:
                x["pct5"], x["pct20"] = _chg(closes, WIN5), _chg(closes, WIN20)
        except Exception:  # noqa: BLE001
            pass

    stocks = [x for s in sectors for x in s["stocks"]]
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, stocks))
    n_ok = sum(1 for x in stocks if x["pct5"] is not None)
    LOG.info(f"  [us windows] 个股 {n_ok}/{len(stocks)}")
    for s in sectors:
        for k in ("pct5", "pct20"):
            vals = [x[k] for x in s["stocks"] if x.get(k) is not None]
            s[k] = round(sum(vals) / len(vals), 2) if vals else None


def fetch_heat_us():
    """新浪 gb_ 批量实时：字段 0名称 1现价 2涨跌幅% 13?市值（实测第13列=市值）。"""
    syms = [s for lst in US_HEAT_SECTORS.values() for s in lst]
    r = _retry(lambda: requests.get(
        "https://hq.sinajs.cn/list",
        params={"list": ",".join("gb_" + s for s in syms)},
        headers={"Referer": "https://finance.sina.com.cn"}, timeout=15))
    r.encoding = "gbk"
    quotes = {}
    for line in r.text.strip().splitlines():
        try:
            key, payload = line.split("=", 1)
            sym = key.split("_gb_")[-1].strip().lower()
            f = payload.strip('";').split(",")
            if len(f) > 12 and f[1]:
                quotes[sym] = {
                    "code": sym.upper(), "name": f[0],
                    "price": float(f[1]), "pct": float(f[2]),
                    "cap": float(f[12]) if f[12] else None,
                    "asof": f[3],
                }
        except Exception:  # noqa: BLE001
            continue
    sectors = []
    for theme, lst in US_HEAT_SECTORS.items():
        stocks = [quotes[s] for s in lst if s in quotes]
        if stocks:
            sectors.append({"name": theme,
                            "pct": round(sum(x["pct"] for x in stocks) / len(stocks), 2),
                            "stocks": sorted(stocks, key=lambda x: -(x["cap"] or 0))})
    if not sectors:
        raise RuntimeError("美股实时行情解析为空")
    asof = max((q["asof"] for q in quotes.values()), default="")
    try:
        _add_windows_us(sectors)
    except Exception as e:  # noqa: BLE001  窗口缺失不连坐主数据
        LOG.info(f"[warn] us 窗口计算失败: {type(e).__name__}: {str(e)[:120]}")
    return {"asof": asof, "sectors": sectors}


def _latest_industry_map():
    """个股→同花顺一级行业映射。

    优先读全站单一来源（data/auction/industry_map.json，industry_common）；
    退回历史缓存文件仅限补抓旧日期的兼容路径。"""
    import industry_common
    shared = industry_common.load_shared_ticker_map()
    if shared:
        # 第二返回值是「映射 vintage」日期（8 位口径），供 heatmap asof 标注；
        # 不能用 "shared" 这类来源标签冒充日期（会原样漏到前端「截至 shared」）。
        return shared, industry_common.shared_map_date8()
    if not os.path.isdir(HT_CACHE):
        return None, None
    files = sorted(f for f in os.listdir(HT_CACHE)
                   if f.startswith("stock_industry_") and f.endswith(".json"))
    if not files:
        return None, None
    fp = os.path.join(HT_CACHE, files[-1])
    with open(fp, encoding="utf-8") as f:
        return json.load(f), files[-1][15:23]


def _thscode(code):
    """6 位代码 → thscode（单一来源 backend/thscodes.py）。

    旧实现只按首位猜市场：5/9 开头（沪基金/沪B）会被误归 .BJ——已收敛修复。"""
    return thscodes.to_thscode(code)


def _compound(pcts, n):
    """最近 n 个交易日日涨跌幅复合成区间涨幅（不足 n 天返回 None）。"""
    if len(pcts) < n:
        return None
    acc = 1.0
    for p in pcts[-n:]:
        acc *= 1 + p / 100.0
    return round((acc - 1) * 100, 2)


def _add_windows_cn(sectors):
    """A股热力图补 5/20 日涨幅。
    行业：复盘快照逐日涨跌幅复合（零远程调用）；
    个股：hithink 前复权日线收盘（仅展示中的各行业成交额前 8 只，8 线程）。"""
    from concurrent.futures import ThreadPoolExecutor
    try:
        bd = _load_board_daily(WIN20 + 12)
    except Exception as e:  # noqa: BLE001
        LOG.info(f"[warn] cn 行业历史缺失: {type(e).__name__}: {str(e)[:100]}")
        bd = {}
    for s in sectors:
        hist = bd.get(s["name"]) or {}
        seq = [hist[d] for d in sorted(hist)]
        s["pct5"] = _compound(seq, WIN5)
        s["pct20"] = _compound(seq, WIN20)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 60 * 86400 * 1000   # ≥ 21 个交易日留缓冲

    def one(x, pause=0.0):
        x["pct5"] = x["pct20"] = None
        if pause:
            time.sleep(random.random() * pause)
        try:
            d = ht.ht("market", "history", "--thscode", _thscode(x["code"]),
                      "--start-ms", str(start_ms), "--end-ms", str(end_ms),
                      timeout=40)
            rows = sorted(d.get("item") or [], key=lambda r: r.get("date_ms") or 0)
            closes = [float(r["close_price"]) for r in rows if r.get("close_price")]
            x["pct5"], x["pct20"] = _chg(closes, WIN5), _chg(closes, WIN20)
            x.pop("_wfail", None)
        except Exception:  # noqa: BLE001
            x["_wfail"] = True

    stocks = [x for s in sectors for x in s["stocks"]]
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(lambda x: one(x, 0.3), stocks))
    retry = [x for x in stocks if x.pop("_wfail", False)]
    if retry:
        LOG.info(f"  [cn windows] 限流重试 {len(retry)} 只")
        time.sleep(5)
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda x: one(x, 0.8), retry))
        for x in stocks:
            x.pop("_wfail", None)
    n_ok = sum(1 for x in stocks if x["pct5"] is not None)
    LOG.info(f"  [cn windows] 个股 {n_ok}/{len(stocks)}")


def fetch_heat_cn():
    ind_map, ind_date = _latest_industry_map()
    rows = ht.market_snapshot_all()
    live = [r for r in rows if r.get("last_price") and r.get("turnover")
            and r.get("price_change_ratio_pct") is not None]
    if not live:
        raise RuntimeError("全市场快照为空")
    try:
        name_map = ht.symbol_names(cache_dir=HT_CACHE)
    except Exception:  # noqa: BLE001
        name_map = {}
    sectors = {}
    for r in live:
        code = str(r.get("ticker") or "")
        sec = (ind_map or {}).get(code) or "其他"
        pct = max(-11.0, min(11.0, float(r["price_change_ratio_pct"])))
        amt = float(r["turnover"])
        d = sectors.setdefault(sec, {"amount": 0.0, "wp": 0.0, "n": 0, "stocks": []})
        d["amount"] += amt
        d["wp"] += pct * amt
        d["n"] += 1
        d["stocks"].append({"code": code,
                            "name": name_map.get(code) or code,
                            "pct": round(pct, 2), "price": r.get("last_price"),
                            "amount": amt})
    out = []
    for nm, d in sectors.items():
        top = sorted(d["stocks"], key=lambda x: -x["amount"])[:8]
        for s in top:
            s["amount"] = round(s["amount"] / 1e8, 2)  # 亿
        out.append({"name": nm, "n": d["n"],
                    "pct": round(d["wp"] / d["amount"], 2) if d["amount"] else 0,
                    "amount": round(d["amount"] / 1e8, 1),  # 亿
                    "stocks": top})
    out.sort(key=lambda x: -x["amount"])
    asof = ind_date or ""
    if len(asof) == 8:
        asof = f"{asof[:4]}-{asof[4:6]}-{asof[6:]}"
    try:
        _add_windows_cn(out)
    except Exception as e:  # noqa: BLE001  窗口缺失不连坐主数据
        LOG.info(f"[warn] cn 窗口计算失败: {type(e).__name__}: {str(e)[:120]}")
    return {"asof": asof, "sectors": out}


# ---------------- 4. 日内分时 ----------------

INTRA_SPECS = {"SHA": "sh000001", "SZA": "sz399001"}   # 本环境仅新浪 CN 分钟线可用（东财/腾讯/雅虎均不通报 404）


def fetch_intraday():
    """最近交易日 1 分钟分时（含多日尾部，取最后一天 + 前收盘）。"""
    out = {}
    for idx_id, sym in INTRA_SPECS.items():
        try:
            r = _retry(lambda: requests.get(
                "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                params={"symbol": sym, "scale": 1, "ma": "no", "datalen": 250},
                headers={"Referer": "https://finance.sina.com.cn"}, timeout=15))
            rows = json.loads(r.text)
            rows = [x for x in rows if x.get("day") and x.get("close")]
            if not rows:
                raise RuntimeError("分钟数据为空")
            last_day = str(rows[-1]["day"])[:10]
            day_rows = [x for x in rows if str(x["day"])[:10] == last_day]
            prev_rows = [x for x in rows if str(x["day"])[:10] < last_day]
            prev_close = round(float(prev_rows[-1]["close"]), 2) if prev_rows else None
            out[idx_id] = {"date": last_day, "prev_close": prev_close,
                           "bars": [[str(x["day"])[11:16], round(float(x["close"]), 2)]
                                    for x in day_rows]}
            LOG.info(f"[intraday] {idx_id} {sym}: {len(day_rows)} 根 @ {last_day}")
        except Exception as e:  # noqa: BLE001
            LOG.info(f"[intraday] {idx_id} FAIL {type(e).__name__}: {str(e)[:100]}")
    return out or None


# ---------------- 主流程 ----------------

def main():
    # 跨进程锁：手动重抓与计划任务不并发（与 server.py /api/fetch-global 共用）
    lock = lockutil.acquire(LOCK_PATH, stale_min=20)
    if lock is None:
        LOG.info("已有全球抓取在运行（锁 fetch-global.lock），本次跳过。")
        sys.exit(0)
    try:
        _run()
    finally:
        lockutil.release(lock)


def _run():
    t0 = time.time()
    errors = {}
    payload = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "date": time.strftime("%Y-%m-%d")}
    # 降级保护：单块失败时沿用上一份同块数据并标 stale，
    # 防止盘前抓取（A股快照为空）把昨日完好的热力图冲成错误态
    try:
        with open(OUT, encoding="utf-8") as f:
            _prev = json.load(f)
    except Exception:  # noqa: BLE001
        _prev = {}

    def _keep(key):
        old = _prev.get(key)
        if old:
            payload[key] = dict(old, stale=True)
            LOG.info(f"[warn] {key} 本次失败，沿用上一份并标 stale")

    try:
        indices, e = fetch_indices()
        errors.update({f"index_{k}": v for k, v in e.items()})
        if not indices:
            raise RuntimeError("全部指数抓取失败")
        payload["indices"] = indices
        LOG.info(f"[ok] indices: {len(indices)} 个")
    except Exception as e:  # noqa: BLE001
        errors["indices"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.info(f"[fail] indices: {errors['indices']}")

    try:
        radar, e = fetch_radar(payload.get("indices") or [])
        errors.update({f"radar_{k}": v for k, v in (e or {}).items()})
        if radar:
            payload["radar"] = radar
            n_us = len(radar["us"]["themes"]); n_cn = len((radar.get("cn") or {}).get("themes") or [])
            LOG.info(f"[ok] radar us={n_us} cn={n_cn}")
    except Exception as e:  # noqa: BLE001
        errors["radar"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.info(f"[fail] radar: {errors['radar']}")

    try:
        payload["heatmap_us"] = fetch_heat_us()
        LOG.info("[ok] heatmap_us")
    except Exception as e:  # noqa: BLE001
        errors["heatmap_us"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.info(f"[fail] heatmap_us: {errors['heatmap_us']}")
        _keep("heatmap_us")

    try:
        payload["heatmap_cn"] = fetch_heat_cn()
        LOG.info("[ok] heatmap_cn")
    except Exception as e:  # noqa: BLE001
        errors["heatmap_cn"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.info(f"[fail] heatmap_cn: {errors['heatmap_cn']}")
        _keep("heatmap_cn")

    try:
        intra = fetch_intraday()
        if intra:
            payload["intraday"] = intra
            LOG.info("[ok] intraday: " + "/".join(intra))
    except Exception as e:  # noqa: BLE001
        errors["intraday"] = f"{type(e).__name__}: {str(e)[:150]}"
        LOG.info(f"[fail] intraday: {errors['intraday']}")

    payload["errors"] = errors
    os.makedirs(OUT_DIR, exist_ok=True)
    Path(OUT).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    kb = os.path.getsize(OUT) / 1024
    LOG.info(f"[done] {OUT}  {kb:.0f}KB  {round(time.time() - t0, 1)}s  errors={len(errors)}")
    if not any(k in payload for k in ("indices", "radar", "heatmap_us", "heatmap_cn")):
        sys.exit(1)


if __name__ == "__main__":
    main()
