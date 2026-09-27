# 架构总览（ARCHITECTURE）

> 一页读懂本项目的结构与数据流。模块级细节以各文件头的「日期决策注释」为准——
> 那些注释记录了每个设计背后的动机与事故，是本项目的一手文档。本文只画地图。

## 定位与形态

单服务、单端口、单入口的个人 A 股盯盘与量化系统：「一套系统 · 盯完一个交易日」。
三种部署形态同一套代码：

1. **本地脚本**：`python start.py` / 双击 `打开看板.bat`（浏览器，默认 8000）
2. **Windows 计划任务 ×8**：无人值守采集 + 备份 + 看门狗
3. **免安装 exe**：PyInstaller onedir/onefile 双形态 + pywebview 桌面窗口（`packaging/`）

## 四层结构

```
┌─ 前端（web/，零构建 MPA）────────────────────────────────────┐
│ 五页：/auction 竞价 · / 轮动 · /recap 复盘 · /global 全球 · /quant 量化 │
│ 共享：lib/（tokens.css 双主题 · theme.js · appbar.js · freshness.js ·   │
│        datepicker.js · util.js · echarts 本地 vendored）                │
│ 数据获取：HTTP 轮询 + ETag/304，无 WebSocket；全局健康胶囊 45s 拉 health │
├─ 服务（server.py，stdlib http.server，无框架）──────────────────┤
│ 正则路由表（GET_ROUTES/POST_ROUTES，唯一真相）→ Handler → 缓存/序列化     │
│ 设计边界：HTTP 层不做业务计算——聚合口径全在派生层/专门模块                │
│ 安全：写接口跨源闸门（Origin/Referer）+ 可选 AK_WRITE_TOKEN/READ_TOKEN；  │
│       /data、/quant-results 白名单防穿越                                 │
├─ 派生与领域逻辑（backend/）──────────────────────────────────┤
│ derive.py 派生层：轮动统计/强度矩阵/情绪指数 → data/*/panel/              │
│ auction/ 竞价（auc_collector 30s 轮询 + auc_alerts 异动回算 + pool_exec） │
│ rotation/ 轮动（ths_collect 同花顺 90 行业逐分钟）                        │
│ recap/ 复盘（fetch_daily 14 模块 + speculate/spec_pool/spec_validate     │
│          投机分析与备选池闭环 + us_close_task 凌晨链）                     │
│ quant/quant_api.py 量化引擎接线（回测/选股/信号/研究长任务）               │
├─ 数据（data/，纯文件，无数据库）────────────────────────────┤
│ JSON/JSON.gz 快照 + CSV 派生面板 + parquet（研究库）；原子写统一 fsutil    │
└─ 运维侧车 ────────────────────────────────────────────────┘
   watchdog.py 每 15min 探活+日志清理 · backup.py 每日滚动 zip ·
   notify.py 三渠道手机推送（health 巡检线程 10min 一扫）· logutil 统一日志
```

## 一条数据流走通（轮动为例）

```
同花顺 881xxx 指数（90 个）
  → ths_collect.py 盘中每 60s 批量快照，累积进 data/rotation/daily/与 intraday/
  → derive.py 重算：stats.json（轮动统计）/ matrix.json（强度矩阵）/ board_cum
  → server.py /api/rotation-stats、/api/rotation-matrix（读面板文件 + ETag）
  → web/index.html 热力图 / 排名轮动画 / 强度矩阵
```

采集→派生→接口→渲染四段各自独立：任何一段重跑都不冒充时点，断档有兜底
（17:10 收盘定格兑底任务）与诚实标注（stale 标记、空态）。

## 单一来源清单（每件事只存一处）

| 关注点 | 单一来源 |
|---|---|
| 出站 HTTP（守卫+重试） | `backend/http_retry.py`（guarded_get 内置 netguard） |
| SSRF 防线 | `backend/netguard.py` |
| 原子写盘 | `backend/fsutil.py`（quant_sim 内有意同构副本，见其 fork 注记） |
| 交易日历 | `backend/trade_cal.py` |
| thscode 映射 | `backend/thscodes.py` |
| 执行层常量（回测口径） | `backend/execution_layer.py` |
| 启动落点 | `backend/landing.py` |
| 复盘模块契约 | `backend/recap/modules.py` |
| 竞价时间线/观察池 | `backend/auction/auc_config.py` 头注释 |
| 告警推送 | `backend/notify.py` |
| 日志 | `backend/logutil.py` |
| 版本号 | `backend/version.py`（+ 根 CHANGELOG.md） |
| 设计 token/主题 | `web/lib/tokens.css` · `web/lib/theme.js` |
| 顶栏页序 | `web/lib/appbar.js` |
| 前端转义 | `web/lib/util.js` esc() |

## 计划任务时序（交易日）

| 时刻 | 链路 | 入口 |
|---|---|---|
| 04:05 | 美股收盘链（us_market 因子 + T-1 备选池重建） | `backend/recap/us_close_task.bat` |
| 08:40 / 17:05 补刷 | 全球总览 | `backend/global/fetch_global.py` |
| 09:14 | 竞价采集（30s 轮询 → 09:25:10 定盘） | `backend/auction/auction_task.bat` |
| 09:25 | 轮动采集（60s 至 15:00 定格）+ 派生层 | `backend/rotation/fetch_day_task.bat` |
| 12:10 | 数据滚动备份（zip 留 10 份） | `backend/backup.py` + `backup_task.bat` |
| 15:00 后 | 竞价每日归档 | auc_collector 内置（auc_archive.py） |
| 17:05 | 盘后复盘（14 模块 + gzip 归档 + 概念周更） | `backend/recap/fetch_task.bat` |
| 17:10 | 轮动收盘兑底 + 派生层重算 | fetch_day_task.bat 兜底段 |
| 每 15min | 看门狗探活 + 备份巡检 + 日志清理 | `backend/watchdog.py` + `watchdog_task.bat` |

计划任务搭建的四个静默坑（npm .cmd shim 要 call、电源策略杀任务、bat 里不写
中文、IdleSettings 杀盘中采集）见根 README「自动任务」节——重装前必读。

## 关键设计取舍（为什么不"升级"）

- **stdlib HTTP 而非 FastAPI**：单机单用户看板，正则路由表 + 「HTTP 层零业务计算」
  已把复杂度关在领域层；框架引入的收益 < 依赖与启动成本。
- **零构建前端而非 Vue/React**：离线可用、双击即用是硬需求；设计 token + 六个
  lib 模块构成小型设计系统，页面内联 JS 正在按域抽出（2026-09 起）。
- **纯文件而非数据库**：快照/面板/研究库全部是可直接查看、可直接备份删除的文件；
  归档与保留策略按目录各自声明（复盘 400 天、轮动 120 天、概念 60 份、任务 30 份）。
- **降级链显式化**：hithink 缺→akshare、研究库缺→腾讯备源、跌停池缺→快照近似，
  降级一律写入快照字段或 health 面板可见——静默降级难归因，是 0914 事故教训。

## 相关文档

- 根 README：安装/使用/计划任务/数据源口径
- `CHANGELOG.md`：版本演进；`backend/version.py`：版本单一来源
- `docs/README-recap.md` / `docs/README-rotation.md`：板块文档
- `quant/docs/design.md`：量化引擎内部设计
- `strategy-iter/自动选股与策略自迭代系统.md`：策略迭代方法论
- `docs/产品优化方案-20260927.md`：本轮优化的完整依据
