# 盘后复盘板块（/recap）

盘后自动抓取全套 A 股复盘数据，网页看板展示：大盘指数、市场情绪、板块涨跌、连板梯队、
涨停/跌停/炸板池、龙虎榜（含游资/机构净买）、热股榜、ETF 风向、监管榜单，以及投机分析
（情绪周期定位/题材核心/偏离值雷达/A 杀监控/明日交易备选池）与复盘笔记。

> 本文档 2026-09-27 重写对齐现行结构（项目合并进统一看板后的路径）。此前版本指向
> `backend/fetch_daily.py`、`backend/serve.py` 等合并前路径，均已失效。

## 数据源

- **主力：hithink-finance CLI**（同花顺口径）：涨停/跌停/炸板池、龙虎榜、热股榜、行业/概念
  指数（快照+历史）、全市场快照、ETF 快照、估值、交易日历。封装见 `backend/recap/ht.py`，
  需安装并认证：`hithink-finance auth status`
- **辅助：akshare**（新浪/东财）：大盘指数、两市成交、大小盘、监管公告、外围市场
- **备源：腾讯前复权日线**（`backend/recap/spec_ohlc_fb.py`）：研究库日线层缺失时兜底
  （2026-09-14 事故引入），缺失行如实标注 `src`
- 全市场快照本地复用：宽度统计/涨跌分布/成交额榜/昨日涨停今日表现共享一次抓取

## 快速开始

```bash
# 1. 抓取今日（收盘后；hithink 交易日历自动跳过非交易日，--force 可强制）
python backend/recap/fetch_daily.py

# 2. 启动统一看板（浏览器打开 http://127.0.0.1:8000/recap）
python start.py
```

## 命令

```bash
# 抓取今天
python backend/recap/fetch_daily.py

# 抓取指定日期
python backend/recap/fetch_daily.py --date 20260805

# 快照保留天数（默认 400 天，更旧的自动清理，封盘库约 176MB）
python backend/recap/fetch_daily.py --keep-days 400

# 历史补抓：自动找 data/recap 缺口，用 hithink 历史接口回补核心模块
# （涨停/跌停/炸板池、行业板块、龙虎榜、监管公告；当日快照类模块无法回补）
python backend/recap/backfill.py
python backend/recap/backfill.py --date 20260825 --force

# 校验快照（全过返回 0）
python backend/recap/check_snapshot.py data/recap/20260805.json.gz

# 凌晨美股收盘链（04:05 计划任务；us_market 因子 + T-1 备选池重建）
python backend/recap/us_close_task.py

# 统一服务（复盘页路由 /recap，亦可 --port 改端口）
python server.py
```

## 自动抓取

Windows 计划任务（bat 在 `backend/recap/`，任务搭建的四个静默坑见根 README「自动任务」节）：

- `fetch_task.bat`：每交易日 **17:05** 快照抓取 + 概念周更 + gzip 归档 + 补刷全球
- `us_close_task.bat`：每日 **04:05** 美股收盘链（us_market 因子 + T-1 备选池重建）

## 数据格式

`data/recap/YYYYMMDD.json.gz` = `{ date, fetched_at, modules: { … }, prev }`

模块清单与字段契约以 **`backend/recap/modules.py`（MODULE_REGISTRY）为单一来源**，当前
14 个：market_indices / breadth / limit_up_pool / limit_down_pool / limit_break_pool /
boards / concepts / extra / hot_stock / etf / global_market / regulatory / lhb / speculation。

- 每个模块：`{"status": "ok", "data": [...]}` 或 `{"status": "error", "error": "真实异常"}`
- 历史补抓的快照含 `"backfill": true`，仅含可回补模块
- 派生面板（情绪指数序列等）在 `data/recap/panel/`；gzip 归档在 `data/recap/archive/`
- 复盘笔记落盘 `data/recap/notes/`（`GET/POST /api/recap/note`），前端 localStorage 仅兜底

## 注意

- 监管榜单 = 当日公告按两类筛选：股票交易异常波动 / 纪律处分·监管措施（纵向两行展示）
- 行业板块：同花顺 90 个一级行业指数（881xxx，历史含当天无滞后）；概念板块 390 个快照
- 涨停/跌停/炸板池、龙虎榜支持点击表头排序；涨停池可按涨停原因搜索
- 投机分析与备选池闭环：`speculate.py`（投机情绪加工）、`spec_pool.py`（备选池）、
  `spec_validate.py`（T-1 逐票验证 + 45 日窗口有界调参 + 样本外漂移监控）
- 输出全 UTF-8；Windows 控制台乱码请设 `PYTHONIOENCODING=utf-8`
- hithink 缓存：行业/概念目录缓存 7 天、行业指数日线增量缓存于 `backend/recap/.ht_cache/`
