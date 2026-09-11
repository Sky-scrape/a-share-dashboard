# -*- coding: utf-8 -*-
"""【LEGACY · 东财口径（2026-09-01 退役）】
板块清单管理（旧）：从东财 clist 接口拉取行业/概念板块清单。整站已统一同花顺口径，
新单一来源是 ths_collect.load_board_pool（index catalog 881xxx 一级 90）。

旧设计备注（防丢档）：轮动池口径（2026-09-01 改版）：行业只保留申万一级（31 个）——
东财行业目录（m:90 t:2，全量 496 个）本身一/二/三级混排，旧版「按市值前 70」会把
「证券Ⅱ+证券Ⅲ」「银行+银行Ⅱ+国有大型银行Ⅲ」这类父子叠层同时选进来，板块重复且过细。
接口失败时使用内置兜底清单（一级真实板块代码，采集时名称以分时接口返回为准）。
"""
import json
import os
import random
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
_BACKEND = os.path.join(PROJECT_ROOT, "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
import fsutil   # noqa: E402  原子写盘单一来源（boards.json 与 ths_collect 双写方同口径）
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "rotation")
BOARDS_FILE = os.path.join(DATA_DIR, "boards.json")

HOSTS = ["push2his.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com"]
# 禁用系统代理：直连东财接口（坏代理会导致请求挂起/随机断连，与 fetch_day 同口径）
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# 申万 2021 一级行业 31 个：轮动行业池唯一口径。名称与东财 f14 字面一致，
# 用于从行业目录里过滤出最粗一级（东财“机械设备”即申万“机械”口径）。
SW_L1_NAMES = [
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "机械设备", "电力设备", "国防军工", "商贸零售", "社会服务",
    "银行", "非银金融", "房地产", "交通运输", "公用事业", "建筑材料", "建筑装饰",
    "传媒", "通信", "计算机", "煤炭", "医药生物", "石油石化", "环保", "美容护理", "综合",
]
SW_L1_SET = set(SW_L1_NAMES)

# 内置兜底清单：申万一级的东财真实板块代码（接口不可用时直接采，代码 2026-09 核对）
FALLBACK_INDUSTRY = [
    ("BK0433", "农林牧渔"), ("BK1206", "基础化工"), ("BK0479", "钢铁"), ("BK0478", "有色金属"),
    ("BK1201", "电子"), ("BK1211", "汽车"), ("BK0456", "家用电器"), ("BK0438", "食品饮料"),
    ("BK0436", "纺织服饰"), ("BK1212", "轻工制造"), ("BK1205", "机械设备"), ("BK1200", "电力设备"),
    ("BK1204", "国防军工"), ("BK1213", "商贸零售"), ("BK1214", "社会服务"), ("BK1283", "银行"),
    ("BK1203", "非银金融"), ("BK1202", "房地产"), ("BK1210", "交通运输"), ("BK0427", "公用事业"),
    ("BK1208", "建筑材料"), ("BK1209", "建筑装饰"), ("BK0486", "传媒"), ("BK1215", "通信"),
    ("BK1207", "计算机"), ("BK0437", "煤炭"), ("BK1216", "医药生物"), ("BK0464", "石油石化"),
    ("BK0728", "环保"), ("BK1035", "美容护理"), ("BK1217", "综合"),
]


def _clist(fs, page=1, size=100, attempts=4):
    """调用 clist 接口单页，多主机容错。注意：服务端每页最多 100 条，size 传大了也没用。"""
    params = {
        "pn": page, "pz": size, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f20", "fs": fs, "fields": "f12,f14,f20,f3,f104,f105",
    }
    for i in range(attempts):
        for host in random.sample(HOSTS, len(HOSTS)):
            try:
                r = requests.get(f"https://{host}/api/qt/clist/get", params=params,
                                 headers=HEADERS, timeout=12)
                d = r.json().get("data")
                if d and d.get("diff"):
                    return d["diff"], d.get("total") or 0
            except Exception:
                continue
        time.sleep(1.5)
    return None, 0


def _clist_all(fs, max_pages=8):
    """分页拉全 clist 目录（东财行业目录 496 个，单页上限 100，必须翻页）。"""
    rows, total = [], None
    for page in range(1, max_pages + 1):
        diff, t = _clist(fs, page=page)
        if diff is None:
            break
        rows += diff
        total = t or total
        if total and len(rows) >= total:
            break
        time.sleep(0.4)
    return rows


def fetch_boards(industry_count=len(SW_L1_NAMES), concept_count=0, refresh=False):
    """拉取板块清单并缓存。行业=申万一级白名单过滤；返回 {"industry": [...], "concept": [...]}"""
    if not refresh and os.path.exists(BOARDS_FILE):
        return json.load(open(BOARDS_FILE, encoding="utf-8"))

    boards = {"industry": [], "concept": []}
    ind = _clist_all("m:90+t:2+f:!50")
    l1 = [x for x in ind if x.get("f14") in SW_L1_SET]
    if len(l1) >= 28:   # 一级基本齐（个别小类改名可容忍），才认为目录可信
        # 同名去重保险（目录理论上唯一），再按总市值降序
        seen = set()
        l1 = [x for x in sorted(l1, key=lambda x: x.get("f20") or 0, reverse=True)
              if x["f12"] not in seen and not seen.add(x["f12"])]
        boards["industry"] = l1[:industry_count]
    else:
        # 目录拉取不完整/失败：用内置一级兜底代码（f14 仅展示用，采集以分时接口返回为准）
        boards["industry"] = [{"f12": c, "f14": n} for c, n in FALLBACK_INDUSTRY[:industry_count]]

    if concept_count > 0:
        con = _clist_all("m:90+t:3+f:!50")
        if con:
            con.sort(key=lambda x: x.get("f20") or 0, reverse=True)
            boards["concept"] = con[:concept_count]

    os.makedirs(DATA_DIR, exist_ok=True)
    fsutil.save_json_atomic(BOARDS_FILE, boards)
    return boards


def load_boards():
    """读取缓存清单；不存在则拉取。"""
    if os.path.exists(BOARDS_FILE):
        return json.load(open(BOARDS_FILE, encoding="utf-8"))
    return fetch_boards()


if __name__ == "__main__":
    b = fetch_boards(refresh=True)
    print(f"行业板块 {len(b['industry'])} 个, 概念板块 {len(b.get('concept', []))} 个 -> {BOARDS_FILE}")
    for x in b["industry"][:5]:
        print(" ", x.get("f12"), x.get("f14"), round((x.get("f20") or 0) / 1e8), "亿")
