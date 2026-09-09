# A股看板（统一项目）

单服务、单端口、单入口的个人 A 股盯盘与策略系统，五大板块按盯盘节奏排序：**实时竞价 → 日内轮动 → 盘后复盘 → 全球总览 → 量化平台**。

- 启动落点跟盯盘节奏：工作日 09:10–09:30 自动先开竞价页，其余时间先开轮动页（`backend/landing.py` 单一口径，不联网）
- 全站行业/板块口径统一为**同花顺一级行业指数（881xxx，90 个）**，跨页精确联动；概念板块保持同花顺概念目录
- Windows 计划任务全自动采集（竞价/轮动/复盘/全球四条定时链路），支持手机 Tailscale 私有访问与全站双主题

## 首次使用（五步走）

1. **装 Python 依赖**

   ```bash
   pip install -r requirements.txt
   ```

2. **装数据源 CLI 并配 key**（不装也能跑：量化引擎、策略自迭代与全部测试离线可用，只是四条采集链路无数据）

   ```bash
   npm install -g @hithink-tech/hithink-finance-cli   # 公开发布于 npm（MIT；源码 github.com/HiThink-Tech/Financial-API）
   hithink-finance auth login                          # 到 https://fuyao.aicubes.cn 申请 API key 后配置
   ```

3. **启动**：Windows 双击 `打开看板.bat`，或项目根目录运行 `python start.py`（工作日 09:10–09:30 自动先开竞价页，其余时间先开轮动页）

4. **首次抓数据**：各页面点「重新抓取」拉当日数据；量化平台页签内直接回测/选股/信号（行情走 hithink 本地缓存，首次任务自动拉取）

5. **全自动采集（可选）**：按下方[「自动任务」](#自动任务windows-计划任务)一节建 Windows 计划任务，五个时段无人值守

浏览器直接访问（默认 8000 端口）：

```
http://127.0.0.1:8000/auction   实时竞价（09:15–09:25 集合竞价，页内可随时补抓）
http://127.0.0.1:8000/          板块日内轮动（根路径入口）
http://127.0.0.1:8000/recap     盘后复盘
http://127.0.0.1:8000/global    全球总览
http://127.0.0.1:8000/quant     量化平台工作台（造策略/回测/选股/信号/研究全部原生页内完成）
```

> **关于 hithink-finance**：主力数据源 CLI 已在 npm 公开发布（`@hithink-tech/hithink-finance-cli`，MIT），克隆者可直接安装；使用前按 https://fuyao.aicubes.cn 流程申请 API key，然后 `hithink-finance auth login` 配置。没有它时，量化引擎（quant/）、策略自迭代与全部测试可完整运行；复盘/竞价/轮动/全球四条采集链路会在抓取期报错（start.py 启动预检会提示）。

## 更新日志（v1.0 · 2026-09-06 首版）

> 首版发布。2026-08-28 以来的全部改动按主题归组如下，逐日开发明细见 git 提交历史。

**时间线**：08-28 轮动+复盘合并 → 08-30 全球总览并入、量化平台整体迁入 → 08-31 实时竞价新增 → 09-01 全站切换同花顺板块口径 → 09-02~03 移动端/监护自愈/投机分析 → 09-04 工程加固/M_Final 备选池 → 09-05 全站双主题 → 09-06 策略 C_Final 收敛、备选池切换 → **v1.0 首版**。

### 五大板块

- **实时竞价（/auction）**：09:15–09:25 逐轮采集（自选 + 昨日涨停池/热股自动组池）；竞价热榜、高开低开分布、板块竞价强度（一级行业指数开盘缺口全成分口径）、强弱转换、涨停接力、高开兑现；异动提醒全天回算（盘后/刷新不丢）；逐轮采集体检面板
- **日内轮动（/）**：板块热力图 + 钻取、蝶形多日轮动动画、双日对比、量价视图、强度矩阵与日内形态、布局预设、导出小结、隔夜外围预判
- **盘后复盘（/recap）**：五叙事组 + 投机分析（情绪周期定位/题材核心识别/偏离值雷达/高位承接 A 杀监控/异动事件/方法论速查）
- **全球总览（/global）**：五城时钟、世界地图点击看走势、中美轮动雷达、美/A 热力图（涨跌停感知色阶 + 自定义钻取）
- **量化平台（/quant）**：造策略/回测/策略库/选股台/今日信号/参数研究（网格排名 + Walk-Forward），多策略对比与自包含 HTML 报告；涨停池与竞价标的可一键送入回测（引擎 115 项回归测试全绿）

### 策略系统（自迭代与备选池）

- **策略自迭代引擎**（strategy-iter）：三轮完整区间迭代收敛 **C_Final**——整体胜率 59.75%、均次日 +2.13%、最大回撤 -1.99%；主梯度是概念内涨停家数
- **概念维度**：概念为主、行业为辅——全市场概念映射（390 概念、7 天新鲜度、机械概念黑名单、成分时点归档），驱动概念 = 所属概念中当日涨幅最高者
- **明日交易备选池**：四档环境分档定配额，涨停组/低吸组双打分 + 次日买点与风险位；执行层常量单一来源（`backend/execution_layer.py`）
- **每日验证自我优化闭环**：T-1 备选池逐票验证与归因（市场/买点/情绪/板块/概念/资金/随机），45 日滚动窗口内有界调门槛与因子惩罚；结构规则/环境配额/执行层永不自动改动，全部留痕可重放
- **样本外追踪 + 20 日到期复审**：调参区间外的每日验证单独累积，胜率回落超阈值自动告警并建议重开迭代——防过拟合的最后防线
- **资金曲线回测**：把条件期望换算成纪律执行的组合曲线（含触发率/开盘入场/空仓日）：累计 +62.03%、年化 +106.87%、最大回撤 -38.85%
- **竞价缺口前瞻回验**：昨日涨停池今日竞价低开占比 >50% 的交易日，备选池均益转负 → 竞价页「情绪前瞻」警戒提示（只作执行层提示，不改规则）

### 工程与可靠性

- **安全**：全部写接口过跨源闸门（Origin/Referer 校验，异源 403）+ 可选 `AK_WRITE_TOKEN`；前端 esc() 单一来源，六处口径不一的转义实现收敛为一
- **单一来源收敛**：交易日历（8 位口径）/thscode 映射/东财常量/HTTP 重试/涨跌区间分桶/执行层常量/顶栏页序/启动落点——每件事只存一处，改一处全站生效
- **服务架构**：server.py 路由表化、进程内缓存全部带锁、静态资源 ETag 长缓存、API gzip（轮动统计 127KB→10.6KB）
- **采集可靠性**：轮动防缺失三件套（死锁监护自愈拉起/盘外定格点并入/采样对齐整分钟），计划任务电源与空闲条件四坑修复，sector 采集退避重试，盘中停滞告警
- **自愈运维**：监护式启动器（服务崩溃自动重启、指数退避），补跑/补抓不冒充时点、数据边界诚实标注
- **移动端**：Tailscale 私有通道（防火墙仅 Private 配置放行）、390px 视口适配、gzip 省流量
- **全站双主题**：晨报（暖纸色）/夜台（深色霓虹）一键切换，canvas 图表经 `web/lib/theme.js` 单一来源取值重绘，多标签页/iframe 自动跟随，选择持久化

## 目录结构（五板块）

```
A/
├── server.py              统一服务（静态 /auction 与 / 与 /recap 与 /global 与 /quant + 全部数据 API；HTTP 层不做业务计算）
├── start.py               一键启动（端口检测 + 按 landing 开浏览器，子进程固定 --no-open 不双开）
├── backend/
│   ├── derive.py          派生层：情绪指数/晋级率/轮动统计/强度矩阵/多日轮动动画 → data/*/panel/（带版本号，单日特征缓存增量计算）
│   ├── landing.py         启动落点单一口径（竞价窗口→/auction，其余→/；server 与 start 共用，可 `python backend/landing.py` 自测）
│   ├── lockutil.py        跨进程文件锁（手动重抓与计划任务不并发）
│   ├── industry_common.py 个股→同花顺一级行业映射的跨子系统读取器（单一来源 data/auction/industry_map.json，超 5 天不可信则退回自建）
│   ├── execution_layer.py 执行层常量单一来源（备选池买点窗口/风险位，speculate 与竞价执行卡共用）
│   ├── recap/             盘后复盘抓取（hithink-finance 主源 + akshare 辅源）
│   │   ├── modules.py     模块契约单一来源（registry + 中文字段容错读取 fget/cget）
│   │   ├── snapio.py      快照读写单一入口（自动兼容 .json / .json.gz 归档）
│   │   ├── providers.py   13 个数据模块（清单引用 modules.MODULES）
│   │   ├── ht.py          hithink-finance CLI 封装
│   │   ├── fetch_daily.py 每日抓取主脚本（文件锁 + 抓完自动重算派生面板）
│   │   ├── backfill.py    历史缺口补抓（hithink 历史接口）
│   │   ├── archive.py     旧快照 gzip 归档（14 天前，约 190KB/份 → ~30KB）
│   │   ├── check_snapshot.py  快照校验（规则引用 modules registry）
│   │   ├── speculate.py   投机分析计算引擎（题材核心/偏离值雷达/高位承接/情绪周期/异动事件 + 明日备选池/验证闭环/优化器）
│   │   ├── concept_map.py 全市场概念映射单一来源（390 概念成分反查 + GENERIC 黑名单 + 时点归档）
│   │   └── fetch_task.bat 定时任务入口（日志→.status/logs/，尾步骤含 derive+archive）
│   ├── rotation/          板块日内轮动采集（同花顺一级行业 90 口径）
│   │   ├── ths_collect.py 轮动采集器：盘中每 60s 批量轮询 881xxx 指数 index snapshot 累积分时（早启等开盘/午间定格续跑/收盘定格退出，盘外补抓只留终态点不造假分钟序列；锁+状态+数据保留）
│   │   ├── config.py / fetch_day.py / collect.py  旧东财口径脚本（LEGACY 退役，仅供归档数据回溯参考，不再接任务）
│   │   ├── fetch_day_task.bat  定时任务入口（现跑 ths_collect 循环 + derive）
│   │   └── backfill_ths_daily.py  历史日K回填器（index.history 十年日线→daily_only 两点点位文件，盘中真实日永不覆盖）
│   ├── global/            全球总览抓取
│   │   ├── fetch_global.py   一次跑出 指数地图/雷达/热力图 → data/global/global.json（文件锁 + 分块降级）
│   │   └── fetch_task.bat    定时任务入口（日志→.status/logs/fetch-global.log）
│   ├── quant/             量化平台板块接线（引擎内置后重构）
│   │   ├── quant_config.py  内置引擎根（A/quant，可 A_QUANT_ROOT 覆盖）+ 任务目录
│   │   ├── collector.py     总览采集：回测流水/报告/研究/策略库存档只读聚合 → /api/quant
│   │   ├── quant_api.py     引擎 API：meta/同步回测/策略库 CRUD/规则代码预览/长任务管理 → /api/quant/*
│   │   └── run_job.py       长任务子进程：screener选股 / signals信号 / grid网格 → data/quant/jobs/
│   └── auction/           实时竞价采集（独立进程，HTTP 层只读）
│       ├── auc_config.py    观察池/时间线/批量上限单一来源（自选 txt + 昨日涨停池与热股榜自动补充）
│       ├── auc_collector.py 采集器：09:15–09:24:30 每 30s live 轮询 → 09:25:10 终态 → 竞价基准
│       ├── pool_exec.py     竞价页「昨日备选池·执行」卡数据（对照冻结执行层实时判定）
│       └── auction_task.bat 定时任务入口（建议交易日 09:14，日志→.status/logs/fetch-auction.log）
├── quant/                 ★ 内置量化引擎（原独立项目整体迁入）
│   ├── quant_sim/           引擎包：core事件撮合（T+1/涨跌停/整手/成本/滑点/参与率）/ data / strategies / metrics / report / research / tools
│   ├── tests/               115 项引擎回归测试：python -m pytest quant/tests -q
│   ├── data/ results/ strategies_store/   行情缓存 / 研究产物 / 策略存档（引擎自管）
│   └── docs/                design.md 架构全文 + changelog.md 演进记录
├── strategy-iter/         ★ 策略自迭代（三轮完整区间迭代收敛 C_Final）
│   ├── engine/              数据/预计算/规则/选股/验证/出轮 引擎（概念与行业双口径单一来源）
│   ├── scripts/             fetch_concepts 概念日线抓取 / backtest_capital 资金曲线回测 / gap_ahead_study 缺口前瞻回验 / analyze_round 轮末分析
│   └── runs/ reports/       各轮产物（stats/picks/validation/资金曲线）与《最终筛选方案》
├── data/
│   ├── auction/           竞价产出：live.json 最新轮 + final.json 定盘 + series.json 当日轮次全量 + rounds_meta.json 逐轮元数据 + benchmark.json 基准 + sector.json 一级行业指数开盘缺口 + industry_map.json 个股→一级行业全量映射 + watchmap.json（行业/名称/来源）+ watchlist.txt（自选清单）
│   ├── recap/             复盘快照（近期 .json，早期 .json.gz）+ panel/（sentiment.csv 等）+ notes/（复盘笔记 .md）+ concept_map.json（概念映射）+ pool_track.json / pool_opt.json（验证与优化器留痕）
│   ├── rotation/          轮动 daily/*.json（同花顺口径，src=ths）+ intraday/（逐轮原始快照）+ panel/（stats.json + matrix.json）+ boards.json；daily_legacy_eastmoney/ 为东财旧口径归档（不接派生层）
│   └── global/            global.json（全球总览：15 指数×250日历史 + 雷达 + 双市场热力图）
├── web/                 五个页面（顶栏顺序 = 盯盘节奏：竞价 → 轮动 → 复盘 → 全球 → 量化）
│   ├── auction/index.html 实时竞价（/auction）：阶段状态条 + 竞价热榜（全量渲染/排序/行业标注）+ 高开低开分布 + 板块竞价强度 + 09:25 定盘 + 强势候选/涨停接力/高开兑现/强弱转换 + 异动提醒 + 采集体检 + 单股竞价曲线弹窗 + 观察池编辑
│   ├── index.html         日内轮动（/，根路径入口）：热力图/回放/榜单/自选/研究面板/板块钻取/双日对比/量价视图/导出小结 + 布局预设 + 新鲜度胶囊；页内 iframe 切复盘
│   ├── recap/index.html   盘后复盘（/recap）：5 叙事组可折叠 + 情绪指数曲线/涨停池/龙虎榜 + 备选池验证与自我优化卡 + 笔记存服务器 + 投机分析
│   ├── global/index.html  全球总览（/global）：五城时钟 + 世界地图 + 中美轮动雷达 + 美/A 热力图 + 走势图三形态（日内分时/折线/日线K线）
│   ├── quant/index.html   量化平台（/quant）：六页签工作台—概览/造策略·回测/策略库/选股台/今日信号/参数研究；多策略对比、自包含报告归档直链、行情缓存新鲜度胶囊
│   └── lib/
│       ├── tokens.css     设计 token + 统一顶栏样式（五页共用，晨报/夜台双主题变量）
│       ├── theme.js       主题状态单一来源（canvas 图表取值 + akthemechange 重绘事件）
│       ├── appbar.js      顶栏导航公共组件（五页单一来源：AKBAR.renderNavgroup + PAGES 页序 + 主题切换）
│       ├── freshness.js   数据新鲜度胶囊（轮动/复盘两页用；竞价页用自己的状态条胶囊）
│       ├── echarts.min.js 图表库（v5.5.0 本地化）
│       └── map/world.json 世界地图 GeoJSON
├── tests/
│   ├── smoke.py           冒烟测试：起临时 server 断言 API 契约 + 前端结构（改完跑这个）
│   └── test_units.py      纯函数单测（环境分档/因子分桶/执行层/日历/重试等）
├── assets/                图标
├── docs/                  各板块文档（README-recap / README-rotation；开发日志 PROGRESS.md 仅本地保留，不入库）
│   └── legacy/            合并前的旧版服务与脚本（仅归档，不再使用）
└── .github/               CI：量化引擎回归测试（data/、.status/、.context/ 为本地运行数据与个人笔记，不入库）
```

## 自动任务（Windows 计划任务）

| 任务 | 时间 | 命令 |
| --- | --- | --- |
| arecap-usclose-fetch | 每日 04:05 | backend/recap/us_close_task.bat → us_close_task.py（等待美股收盘+20min，DST 感知；us_market.py 抓隔夜美股 → speculate --date 上一交易日（备选池含 C6 隔夜美股闸门）→ 快照校验 → derive，日志 .status/logs/usclose.log）。C6 时序改造后**复盘完成时点**：T 日池在 T+1 美股收盘后 1 小时内生成 |
| areauction-live-fetch | 交易日 09:14 | backend/auction/auction_task.bat（09:15–09:24:30 每 30s live 轮询 + 09:25:10 终态 + 基准，日志 .status/logs/fetch-auction.log）；已设为**不看电池、错过可补跑、上限 PT30M** |
| rotation-intraday-fetch | 交易日 09:25 | backend/rotation/fetch_day_task.bat → ths_collect.py 盘中逐分钟轮询循环（数据只到 15:00，收盘定格后自退）；不看电池、错过可补跑、上限 PT8H |
| arecap-daily-fetch | 每日 17:05 | backend/recap/fetch_task.bat（C6 改造后只做 A 股数据落盘：抓取 + hithink data sync（附属，失败仅记 [warn]）+ 概念周更 + gzip 归档，日志 .status/logs/fetch-recap.log；备选池在次日 04:05 链完成） |
| rotation-daily-fetch | 每日 17:10 | backend/rotation/fetch_day_task.bat（盘中断档时的收盘定格兑底 + derive 重算；盘外定格不覆盖已有盘中数据） |
| aglobal-daily-fetch | 工作日 08:40 | backend/global/fetch_task.bat → fetch_global.py（全球指数/雷达/热力/分时 → data/global/global.json）；08:40 = 美股凌晨收盘后、A股盘前窗口 |

> 表内按一天里的触发时刻排序，与顶栏板块顺序（竞价 → 轮动 → 复盘 → 全球 → 量化）同调。手动重抓与计划任务共用文件锁（.status/fetch-*.lock），不会并发。

```bash
schtasks /Create /TN areauction-live-fetch /SC DAILY /ST 09:14 ^
  /TR "<项目根目录>\backend\auction\auction_task.bat"
```

**自动任务四个静默坑（实挂排查记录，重装/新建任务前必读）**：

1. **npm 包的 .cmd shim 在 bat 里必须 `call` 调用**：`hithink-finance` 实为 npm 生成的 `.cmd`，直接写命令名会被它接管，**父 bat 从此行往后所有步骤静默断掉**。bat 内调子脚本/shim 一律加 `call`，关键步骤退出码用 `set RC_x=%ERRORLEVEL%` 隔离，末尾按主链路显式 `exit /b`。
2. **电源策略会杀任务**：新建任务默认「电池模式不启动/拔电中止」，笔记本电池供电时实挂秒死（LastTaskResult=255，日志一行不写）。所有任务应改为**不看电池 + StartWhenAvailable 补跑 + 执行时限**；用 `schtasks /Create` 重建会丢这套配置，必须重新补。
3. **bat 里不要写中文**：cmd 按 GBK 解析 UTF-8 多字节注释会吞行尾换行、把下一行并入注释（实测导致任务 255 早退且首行 echo 都不落盘），bat 应为纯 ASCII；需要中文说明时写在本 README 里。同类教训：PowerShell 5.1 读无 BOM 的 UTF-8 `.ps1` 也按 GBK 解，修复脚本一律纯 ASCII 或 UTF-8 带 BOM。
4. **空闲条件杀盘中采集任务（最隐蔽）**：任务 XML 里 `<IdleSettings>` 意味着“机器空闲满 10 分钟才启动、用户回来就杀”。盘中时段用户在盯盘＝永不空闲→全天一次不跑、日志零输出。修复：导出 XML 移除 IdleSettings（StopOnIdleEnd=false）后 `schtasks /Create /XML /F` 覆盖；S4U 登录类型需管理员执行。新建任务务必检查「条件」页：不勾「仅当计算机空闲时才启动任务」。

另：hithink 本地 DuckDB sync 的内存上限默认 `min(1GiB, 总内存×25%)`，库变大后 commit 会报 failed to pin block；fetch_task.bat 已 `set HITHINK_FINANCE_DUCKDB_MEMORY_LIMIT=4GiB`（官方环境变量入口），重建或换机时记得同样带上。

## 开发约定

- **改完必跑**：`python tests/smoke.py`（API 契约 + 前端结构断言，含量化接线与竞价 API）；引擎级回归另跑 `python -m pytest quant/tests -q`（115 项）。
- **交易日历日期一律 8 位**（`20260901`）：`market.calendar` 返回的就是 8 位，所有消费方均按 8 位比较；拿 `"%Y-%m-%d"` 去比不会报错，只会**静默把每个交易日判成休市**。日历探测「不确定」要返回 None（继续采集）而不是 False。
- **顶栏板块顺序**：只改 `web/lib/appbar.js` 的 `PAGES`（全站单一来源），server.py 路由文档与启动横幅、README 需同步同序；顺序不等于路由映射。
- **启动落点**：只改 `backend/landing.py`（server.py 与 start.py 都 import 它）；不要把时间判断拷回两个入口。
- **竞价能力只加采集器不改 HTTP 层**：观察池合并、时间线、批大小均在 `backend/auction/`，server 只读 `data/auction/` 产出并做序列化。
- **量化能力只加 API 不加 HTTP 层逻辑**：引擎语义（成本/撮合/信号）一律在 quant/quant_sim 内（与 CLI 同口径同测试），server 只做参数校验与序列化；分钟级任务走 run_job.py 子进程（同步回测限 30 标的）。
- **新增/删模块**：只改 `backend/recap/modules.py` registry，其它处自动对齐。
- **情绪指数权重**：在 `backend/derive.py` 的 `W`/`WEIGHTS_VERSION`，改公式必须递增版本号，历史面板可回溯。
- **快照只走 `snapio`**，不直接 glob/open（gzip 归档兼容）。
- **复盘笔记**存 `data/recap/notes/YYYYMMDD.md`（API 读写），浏览器 localStorage 仅作写失败兜底与迁移来源。

## 数据源

- **复盘**：主力 hithink-finance CLI（同花顺口径：涨停/跌停/炸板池、龙虎榜、热股榜、行业/概念指数、全市场快照、ETF、估值、财务、交易日历）；akshare 辅助（大盘指数、两市成交额、监管公告、外围）
- **投机分析**（复盘⑤）：本地 DuckDB 十年日线（`v_daily_qfq` 全市场偏离值/高位承接今日涨跌幅）+ index.history（偏离基准，本地缓存）+ `special anomaly-list`（异动事件，today-only）；其余全部由既有快照现算，零额外抓取
- **轮动**：同花顺一级行业指数（881xxx，90 个）：盘中逐分钟 `index snapshot` 批量轮询累积分时（一次拿全池）；hithink 无分钟级指数接口，漏采不可回补；东财 BK 旧数据存 `daily_legacy_eastmoney/` 仅供回溯
- **竞价**：同花顺 hithink-finance CLI（`market auction-snapshot --stage live|final` 分批 ≤90 只 + `market auction-benchmark` 短期基准 + `market calendar` 交易日校验）；接口为 today-only，当日历史由本站采集器自存 series.json；观察池 = 自选 txt + 最近复盘快照涨停池/热股榜自动补充。
  **关键口径（已实测）：集合竞价结束后，`stage=live` 与 `stage=final` 返回完全相同的 09:25 定盘冻结值（`auction_phase=closed`），只有 `last_price` 随盘中变。** 所以「竞价过程曲线」只能靠 09:15–09:25 本地逐轮留存拿到，事后补抓画不出过程；每轮因此带 `in_window` 标记。
- **量化平台**：引擎内置 `quant/`（无硬编码路径，换机即用）；行情用 hithink 本地 DuckDB（个股秒级）+ 远端 fund.history（ETF），与复盘板块共享 hithink 基础设施；选股因子缓存 quant/data/screener/，任务临时产物在 data/quant/jobs/
- **全球总览**：新浪通道为主（环球指数/美股指数/美股个股实时；本网络环境东财 push2 系列接口不通，勿用）；美股个股 5/20 日窗口直接解析新浪 staticdata 日线；A股雷达动量复用本地复盘快照逐日复合，A股热力图行业窗口用快照复合、个股窗口用 hithink 前复权日线（限流自动重试）；世界地图 GeoJSON 已本地化在 web/lib/map/
- **研究库**：hithink 本地 DuckDB（`hithink-finance db query` 可查 10 年日线），每日 data sync 增量

详细文档见 `docs/README-recap.md`（复盘）与 `docs/README-rotation.md`（轮动）；实时竞价的接口契约与时间线参数以 `backend/auction/auc_config.py` 顶部注释为单一来源。

## 免责声明

本项目仅用于个人学习与技术交流。所有行情数据来自第三方公开接口（同花顺 / 东财 / 新浪等），不保证数据的准确性与完整性；项目内容不构成任何投资建议，据此操作风险自负。
