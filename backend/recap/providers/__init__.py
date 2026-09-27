# -*- coding: utf-8 -*-
"""A股盘后数据抓取模块（providers 包）——对外 API 与拆分前的 providers.py 完全一致。

2026-09-27 拆包（原单文件 944 行 → 按数据域分五个子模块）：动机是单文件持续
生长已到难以整体阅读的规模，按「共享底座 / 池子 / 板块概念 / 大盘与外围 /
榜单与投机」切开后每片职责单一。拆分纪律：

  - 对外 API 不变：`import providers` 与 `providers.<符号>` 的全部现有用法
    （14 个模块函数、prev_zt_today / fetch_all / set_context、PCT_BINS /
    ETF_LIST、MODULES / DATE / HISTORICAL、ak / ht 与 _retry / _round2 /
    _cache_dir / _get_concepts 等私有 helper、各按 DATE 缓存 dict）全部在本包
    命名空间 re-export，零改名、零行为变化。仅不再转发纯标准库/第三方转发名
    （sys / time / json / numpy 等，仓库内无 providers.sys 这类调用点）。
  - 拆分原则：共享取数底座（重试/错误封装/类型归一/按 DATE 缓存/hithink 取数
    原语）沉到 _common.py；涨停/跌停/炸板池归 pools.py；行业/概念归 sectors.py；
    大盘指数/市场宽度/监管公告/外围市场归 market.py；热股/龙虎榜/ETF/涨跌分布
    与成交额TOP20/投机组装归 hotlists.py。
  - DATE / HISTORICAL 的真身留在本包 __init__（set_context 是唯一改写入口），
    子模块经 `import providers` 在调用时读包属性——外部即便直接改写
    providers.DATE，语义也与拆分前的模块级全局一致（这是不用 from-import
    快照绑定的原因）。
  - 子模块间共享符号一律从本包取（from ._common import ... / from .pools
    import ...）；对 ht / http_retry / fsutil 等扁平顶层模块的依赖沿用原
    sys.path 引导语义（fsutil / industry_common 的 backend 根兜底在 _common）。

子模块地图：
  _common.py   共享底座：akshare 守卫、_retry/_err、类型归一、按 DATE 缓存、
               全市场快照/三池/行业/概念目录等 hithink 取数原语、PCT_BINS
  pools.py     涨停/跌停/炸板池 + 昨日涨停今日表现（晋级率原料）
  sectors.py   行业板块 boards + 概念板块 concepts
  market.py    大盘指数 / 市场宽度 breadth / 监管公告 / 外围市场 global_market
  hotlists.py  热股榜 / 龙虎榜 / ETF 风向 / 涨跌分布与成交额TOP20 / 投机组装

数据源（2026-08-28 起）：主力数据源切换为 hithink-finance CLI（同花顺口径，
封装见 ht.py）——涨停/跌停/炸板池、龙虎榜、热股榜、行业/概念指数、全市场快照、
ETF 快照、估值；akshare（新浪/东财）保留用于大盘指数、两市成交额、大小盘、
监管公告与外围市场。每个函数返回 {"status": "ok", "data": ...} 或
{"status": "error", "error": "真实异常类型: 信息"}。
一个模块失败不连坐其他模块；每个模块失败自动重试 2 次（共 3 次尝试，间隔 3 秒）。
"""
import os
import sys
import time

# 禁用系统代理：直连东财/新浪接口（坏代理会导致请求挂起重试）。
# 原 providers.py 的 import 期副作用，保留在包入口，保证任何子模块导入前先生效。
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("NO_PROXY", "*")

# 2026-09-04 起不再在模块级 reconfigure stdout：本包会被宿主进程 import
# （smoke/server 链路），导入即改写宿主输出编码是 auc_collector 踩过的坑。
# 作为脚本直跑时由调用方（fetch_daily/backfill/backfill_full）自行 reconfigure。

# sys.path 引导兜底（原 providers.py 中部同款）：`from modules import MODULES`
# 与子模块的 `import ht` 等扁平顶层导入依赖 recap 目录在 path 上——能导入本包
# 即已在，这里沿用原文件的显式兜底，不改变语义。
_RECAP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECAP_DIR)

# 当前抓取日期，由 set_context 设置（fetch_daily.py / backfill.py 调用），格式 8 位 "20260805"。
# 2026-09-04 收口：此前允许外部直接改写 providers.DATE/HISTORICAL，5 个按 DATE 键的
# 缓存隐性依赖这个全局正确；现在统一走 set_context，谁改写、何时改写一目了然。
DATE = ""

# 历史补抓模式：DATE 非当天时为 True，快照当日接口（成交额补齐等）自动降级
HISTORICAL = False


def set_context(date, historical=None):
    """设置本次抓取的日期上下文（外部唯一改写入口）。

    date: 8 位 "20260805"；historical: DATE 非当天时 True，None=自动推断。"""
    global DATE, HISTORICAL
    DATE = str(date or "")
    if historical is None:
        historical = DATE != time.strftime("%Y%m%d")
    HISTORICAL = bool(historical)


# ------------------------------------------------------------ 共享底座 re-export
from ._common import (  # noqa: E402
    ak, ht, http_retry, index_hist_cache, fsutil,
    require_ak, _retry, _err, _round2, _native, _norm_code, _records,
    _RETRY_TIMES, _RETRY_DELAY,
    _date_cached, index_spot_sina, _cache_dir,
    _get_market_snapshot, _snap_by_code,
    _get_zt_pool_ht, _get_dt_pool_ht, _get_zb_pool_ht,
    _get_industries, _get_industry_map, _get_concepts,
    PCT_BINS,
    _HT_ZT_CACHE, _HT_DT_CACHE, _HT_ZB_CACHE, _HT_HOT_CACHE, _HT_LHB_CACHE,
    _HT_CONCEPTS_CACHE, _INDEX_SPOT_CACHE, _MKT_SNAP_CACHE,
)

# ------------------------------------------------------------ 模块实现 re-export
from .pools import (  # noqa: E402
    prev_zt_today, limit_up_pool, limit_down_pool, limit_break_pool)
from .sectors import boards, concepts  # noqa: E402
from .market import (  # noqa: E402
    market_indices, breadth, regulatory, global_market, _em_global_quotes)
from .hotlists import (  # noqa: E402
    extra, hot_stock, etf, lhb, speculation, ETF_LIST,
    _get_hot_ht, _get_lhb_ht, _symbol_name_map, _current_report,
    _financial_indicators)

# 模块清单单一来源：backend/recap/modules.py（新增/删除模块只改那里）
from modules import MODULES  # noqa: E402


def fetch_all():
    """抓取全部模块，返回 {模块名: 结果}。"""
    return {name: getattr(sys.modules[__name__], name)() for name in MODULES}


# 简单自检（不设 DATE 时直接跑，用于人工探测）：拆包后迁到 __main__.py，
# 在 recap 目录（或 recap 入 path 时）`python -m providers` 等价于旧的
# `python providers.py` 直跑。
