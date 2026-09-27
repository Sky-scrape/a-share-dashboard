# 日内轮动板块（/）

用真实行情数据呈现 A 股行业板块**日内强弱排名轮动**的交互式仪表盘：板块热力图 + 钻取、
排名轮动画、双日对比、量价视图、强度矩阵与日内形态、四种布局预设、日内回放播放器、导出小结。

> 本文档 2026-09-27 重写对齐现行结构。此前版本描述的是合并前「两个板块」架构与已退役的
> 东财采集口径（fetch_day.py / collect.py），命令均已失效。

## 快速开始

```bash
# 一键启动统一看板（单服务、单端口、单入口，默认 8000）
python start.py
# 或 打开看板.bat

# 手动启动服务（日内轮动 = 根路径 /，浏览器打开 http://127.0.0.1:8000/）
python server.py
```

## 采集链（现行：同花顺口径，2026-09-01 起）

- **盘中采集**：`python backend/rotation/ths_collect.py` —— 同花顺一级行业指数
  （881xxx，90 个）盘中逐分钟快照累积，09:25 起每 60s 一轮、15:00 定格。
  计划任务 `fetch_day_task.bat`（backend/rotation/）09:25 触发，采集完成后自动跑
  `backend/derive.py` 重算派生面板
- **收盘兜底**：17:10「轮动兑底」任务——盘中断档时的收盘定格兜底 + 派生层重算
- 常用参数：`--interval 60`（轮询秒）/ `--once`（只采一轮）/ `--date YYYY-MM-DD`（测试）/
  `--force`（非交易日也采）/ `--refresh-boards`（强制刷新板块池）/ `--keep-days 120`
  （daily 与 intraday 超 120 天自动清理）
- **派生层**：`python backend/derive.py` —— 从分时快照重算轮动统计（panel/stats.json）、
  板块强度矩阵与日内形态（panel/matrix.json）、板块累计强度 board_cum、情绪指数；
  HTTP 层不做业务计算，看板只读面板文件

## 数据存储

```
data/rotation/
├── boards.json            # 板块池清单（同花顺一级行业 90 个；ths_collect.load_board_pool 单一来源）
├── daily/                 # YYYY-MM-DD.json 当日板块分时（盘中逐分钟快照累积）
├── intraday/              # 原始逐轮采样（与 daily 同口径清理）
├── panel/                 # 派生面板（stats.json / matrix.json，derive.py 产出）
├── daily_legacy_eastmoney/        # 东财旧口径归档（只读）
└── daily_singlepoint_backup/      # 盘外定格点备份
```

## LEGACY（东财旧口径，2026-09-01 退役）

`fetch_day.py` / `collect.py` / `config.py` 为旧东财口径脚本，**仅供归档数据回溯参考，
不再接任何计划任务与链路**；`daily_legacy_eastmoney/` 为旧口径归档数据。
`backfill_rotation.py` 维持该归档的近期连续性（依赖 config.fetch_boards）。

## API（server.py 路由表）

`/api/dates`（日期列表）· `/api/day?date=`（当日板块分时）· `/api/boards`（板块清单，src=ths）·
`/api/rotation-stats`（轮动统计）· `/api/rotation-matrix`(强度矩阵 + 日内形态) ·
`/api/board-members`（板块钻取）· `POST /api/fetch-rotation`（后台补抓，文件锁防重入）

## 注意

- 板块口径全站统一为同花顺一级行业（90 个）：轮动采集、竞价板块强度、复盘行业模块、
  个股行业归属 industry_map.json 全部同一目录同一名单
- 采集器内置限速（批量轮询 + 指数退避重试 + 错误隔离），单板块失败不影响整体；
  盘中停滞由 server 健康巡检告警（11:30–13:05 午休豁免）
- 前端无数据时给出诚实空态（不造假演示数据）；回放播放器支持时间轴拖动 / 变速
