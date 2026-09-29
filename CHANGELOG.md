# 更新日志（Changelog）

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号单一来源
`backend/version.py`，发布时打同名 git tag `vX.Y.Z`。逐日开发明细见 git 提交历史。

## [1.3.3] · 2026-09-30 · 对抗性审查修复批

8 路红队子代理（服务层/后端基础设施/竞价/采集/量化引擎/前端六个分片 + 安全渗透 +
资金口径两个跨切面）对全项目发起对抗性审查，共报告 43 个问题；每条由独立防守子代理
读代码驳斥复核——确认 42、驳回 1；39 条已最小改动修复（3 条同根因合并处理），修复后
283 项回归 + 出站守卫全绿，终审员换人复核全部改动未发现新引入问题。

- **回测资金口径**：回撤熔断改「收盘决策、次日开盘撮合」——原实现凭当日收盘权益
  触发熔断、清仓单却按当日开盘价成交（未来函数，实测单日净值虚高约 33%）；hfq 后
  复权方向做反（输出与 qfq 全等）重写锚点逻辑；停牌复牌日昨收回退当日收盘致封板
  判定失效（pre_close 兜底链补 ffill）；`Contract.limit_overrides` 带交易所后缀
  代码 KeyError；`buy(percent=)` 改按总权益口径与 target_percent/rebalance 一致；
  signals「参考现价」列由不存在的 `panel.bar` 恢复为 `price_at` 有值
- **信号数据新鲜度**：盘后信号管线 `ht_load` 补 `end=今天`，堵「end 缺省视为不设
  右界」的缓存快路——执行清单不再可能在数周前陈旧数据上静默生成；报告顶部加数据
  陈旧醒目告警（>4 自然日先刷新再下单）
- **数据完整性**：backfill 缺口探测改走 `snapio.list_dates`（.json 与 .json.gz 归档
  并为同一日期键），归档日不再被当缺口重抓出 6 模块半成品遮蔽完整归档，`--force`
  亦不覆盖归档；backfill 全模块失败不再落盘（保持缺口可自动重试，退出码 1）；
  backfill_full「昨日两市成交额」日期格式失配恒空修复；sectors 历史补抓模式不再用
  实时行情冒充历史日板块价与涨跌幅；fetch_daily「昨日涨停」日期标签与取数来源对齐；
  auc_collector 补抓加交易日 fail-closed 闸门 + 归档产物内部日期比对 + 终态异常
  轮数不再虚报
- **安全**：server 入口统一 Host 头白名单（回环名 / 本机网卡地址 / `AK_ALLOWED_HOSTS`）
  封 DNS rebinding 对 CSRF 闸门的整体绕过（CWE-350）；0.0.0.0 绑定且未配
  `AK_WRITE_TOKEN` 时自动生成 `.status/lan_token` 随机写令牌——局域网脚本直连写
  不再零鉴权（显式环境变量永远优先，读面保持 README 显式开启口径避免锁死手机看板）；
  do_POST 补读闸门（配 `AK_READ_TOKEN` 后 POST 数据接口不再绕过）；写 token 比较改
  `hmac.compare_digest` 且访问日志对 `?token=` 脱敏；POST body 32MB 上限（413）+
  32 连接并发上限；netguard check_url 补端口与 userinfo 校验；`tools/check_outbound.py`
  扫描面扩至 requests 别名导入 / urllib.request / httpx
- **并发与资源**：`lockutil` 过期锁接管重构为「内核字节锁仲裁 + 内容世代锚定 +
  原地改写」，消除 TOCTOU 双持（8 进程压测稳定区 0 双持；调用方接口签名不变）；
  共享行情 parquet 收口原子写 + manifest 读改写加跨进程锁（双进程压测 300 条零丢失，
  修复前丢 38%）；`_spawn_fetch` 409 响应移出 `_PROC_LOCK`（不再被慢客户端拖死全部
  抓取接口与 /api/health）；keep-alive 下拒绝路径统一预读请求体（不再污染同连接
  下一请求）；derive.py 循环变量遮蔽致快照缓存永不命中（每轮全量重读）修复；
  quant_api 任务清理不再误删运行中任务的结果文件
- **前端诚实性**：loadDay 瞬时失败不再把真实行情整体替换为演示数据（三分支诚实
  降级 + 盘中自动恢复探测，轮询死锁解除）；autoTick 与 recap 快照加载加过期响应
  守卫（不再把历史日视图拽回今天/旧日期覆盖新日期）；global 页重抓失败不再弹
  「全球数据已更新」假成功（六态如实提示）；freshness 弹层 document 级监听器泄漏
  修复；竞价执行卡盘前/错日拒绝时前端 ⚠ 警示明示
- **运维自愈**：watchdog 健康探测改 `st.update`（`autostart=true` 不再被静默抹除、
  自愈拉起不再必然自关）；app_config 各保留天数钳制 ≥1（配置手滑写 0/负数不再在
  下一计划任务周期不可逆清库）；notify `min_interval_hours` 非数字不再炸穿
  「send 绝不抛错」契约
- 附带收编：`打开看板.bat` 编码修复与 `py` 启动器（本批审查开始前工作区既有改动）

## [1.3.2] · 2026-09-28 · hithink 定位修复批

- **根因修复（PATH）**：注册表用户 PATH 补回 `%APPDATA%\npm`——该目录此前被一次
  整段粘贴的 PATH 覆盖丢失，开机自启/计划任务拉起的进程（只带注册表 PATH）因而
  定位不到 `hithink-finance` CLI：启动横幅告警「未找到 hithink-finance」，
  复盘/竞价/轮动抓取全断
- **定位单一来源**：新增 `backend/hithink_cli.py` `find_exe()`——PATH 优先、
  兜底显式查 `%APPDATA%\npm`；`start.py` 预检与 `backend/recap/ht.py` 迁入同一
  口径；quant 桥接（`quant_sim/data/hithink.py`，独立包根无法平级导入）内联同款
  `_find_exe()`
- **常驻回归**：`test_hithink_cli_find_exe` / `test_find_exe_fallback` 覆盖
  PATH 正常 / 兜底命中 / 真未安装 三分支（离线，不依赖真实 CLI）

## [1.3.1] · 2026-09-27 · 拆分收尾批

- **O-3c 推广至全部五页**：继复盘页之后，轮动（/）、竞价、全球、量化四页的内联 JS
  以同一纪律抽出为各页 `app.js`（逐字符保留 + 字节级对账，CRLF 保持）；web/ 静态根
  直接下发（`/app.js`、`/auction/app.js`…），零构建架构不变
- **smoke 页面源检查泛化**：`_page_src()` 读「index.html + 同目录 app.js」拼接源，
  对检查目标在 html 还是 js 不敏感
- **S-4 断源演练固化**：新增常驻回归 `test_hithink_down_drill_and_recovery`——桩掉
  ht CLI 模拟 401 断源 → probe 如实记录 → `hithink.down` 告警含 `auth login` 处置指引
  → 恢复后告警消失

## [1.3.0] · 2026-09-27 · 版本与治理批

按 `docs/产品优化方案-20260927.md` 三批落地（版本管理 / 防线加固 / 结构拆分）：

- **版本单一来源**：新增 `backend/version.py`；`/api/health` 顶层返回 `version`；桌面窗口
  标题与启动页显示版本；`打包exe.bat` 产物自动带版本号；git tag 与 `__version__` 对齐规范
- **CHANGELOG 建立**：本文件；README 更新日志节改为「最近一版 + 指向」
- **SSRF 守卫收拢（O-1）**：`backend/http_retry.py` 新增 `guarded_get`（netguard 校验 +
  重试单一入口，禁跟随重定向）；存量裸 `requests` 调用点迁移（em_common / fetch_global /
  providers / rotation 回补）；`tools/check_outbound.py` 入 CI 挡住未来绕过
- **CI 增强（S-6）**：pytest 带 `pytest-cov` 覆盖率统计（backend + quant_sim，Linux 腿上传
  coverage.xml）+ 出站守卫检查
- **日志收敛（O-2）**：`logutil` 新增 `get_file_logger`（stdout+文件复合）；notify / watchdog
  两份手写 `_log` 并入；CLI 报告类脚本保留 print
- **数据目录保留策略（O-4）**：`.status/logs` 杂项日志留 30 天（watchdog 顺带清理，超期按
  mtime 删、子目录不动）。核实结论：rotation 分时已有 120 天清理、screener 缓存已有
  「最近 3 份」修剪（`_prune_caches`），无需改动——方案中这两项按实证销项
- **hithink 健康一等公民（S-4）**：`/api/health` 新增 `hithink` 段（auth status 探活，10 分钟
  TTL 缓存 + 后台刷新，失败推手机告警）；README 新增排障表（症状/病因/处置）
- **结构拆分（O-3，架构形态不变）**：`backend/recap/providers.py`（944 行）拆为 `providers/`
  包（门面全量 re-export，对外 API 零变化）；`server.py`（1622 行）拆为 `server_context +
  8 个 handler mixin`（路由表仍集中 server.py，正则逐字未动）；复盘页内联 JS（1883 行）抽为
  `web/recap/app.js`（路由 `/recap/app.js`，ETag 协商缓存）；`tests/smoke.py` 的源码防回退
  检查同步改为读「server 全家桶 / 复盘页 html+js / providers 包」拼接源
- **打包同步**：`ak-dashboard.spec` datas 与 `launcher.py` SYNC_FILES 增列 server 拆分新模块；
  server.py 头部加自目录 sys.path 锚定（onefile 打包态可 import）
- **配置薄层外部化（S-5）**：新增 `backend/app_config.py` + 可选 `.status/config.json`
  （保留天数/留存上限 4 键，缺文件/坏 JSON 回代码默认，不热生效）；接入 fetch_daily /
  ths_collect / watchdog / quant_config；执行层常量保持代码单一来源不外部化
- **文档修复（M-1）**：`docs/README-recap.md`、`docs/README-rotation.md` 重写为现行路径与
  命令（原文档指向已删除的 `backend/serve.py` 等）；README 新增「手机看板」节（O-6）与
  「hithink 排障」表；新增 `docs/ARCHITECTURE.md` 一页架构总览（S-3）
- **归档与减负（D 批）**：一次性修复脚本迁 `docs/legacy/`；`dist/` 旧构建产物清理（仅留
  最新版本化 zip）；strategy-iter/runs 经复核为 **git 跟踪的审计证据链**，撤销原计划的
  zip 归档、补 `strategy-iter/README.md` 写明保留规则（不删任何轮次，磁盘清理只针对
  不入库的 data//raw/）；`.zcode/` 入 ignore
- **fsutil fork 注记（M-2）**：backend 侧文件头补与 `quant_sim/core/fsutil.py` 的有意副本
  声明与分叉点说明（quant_sim 独立包边界不变）
- **UI 细节（O-5）**：`--text-4` 两主题对比度修正（约 1.9–2.2:1 → 2.5–2.9:1，随背景
  token 而异）；双红分工（`--up` 色块 / `--red2` 文字）在 tokens.css 注明决策保留；
  theme.js 回退值同步

## [1.2.0] · 2026-09-23 · exe 发布批

- **免安装 exe 双形态**：PyInstaller onedir（解压版）+ onefile（单文件版，源码与数据常驻
  `%LOCALAPPDATA%\ak-dashboard`，按构建指纹增量同步且用户数据永不动）；`打包exe.bat` 一键双打
- **桌面窗口**：pywebview + WebView2（`packaging/gui.py`）——双击即用不弹浏览器、内联启动页
  防白屏、窗口几何记忆、标题栏随主题染色、关窗即整树退出；`--web` 回退浏览器模式
- **板块累计强度口径统一**：board_cum 由后端单一来源下发（此前前端 Top10 自算与后端全板块口径漂移）
- **跌停池上游故障降级兜底**：ST 票上游整体 500 时以全市场快照 |pct|≥9.9 近似并标注口径
- **行业指数 .TI 双拼修复**；**竞价每日归档**上线（`auc_archive.py`，六件归档 + health 可见）
- **LU 止损口径对齐**；满仓单调 / 蝶恋连板对照回测落库

## [1.1.0] · 2026-09-15 · 告警闭环批

- **告警闭环**：「打开看板才知道」→「手机先知道」——server 内置健康巡检线程（10 分钟）+
  Server酱/企业微信/Telegram 三渠道推送（key 级 6h 节流）；watchdog 计划任务探活死人兜底；
  数据滚动备份（12:10，留 10 份）
- **0914 事故修复**：上游 release 竞态致研究库日线整层缺失——新增腾讯前复权备源
  `spec_ohlc_fb`、层探测落 `.status/duckdb.json` + health duckdb 段、量能口径校验拒用错日数据
- **轮动防缺失**：午休误自愈豁免、lockutil 按持有者 PID 校验、盘外定格点并入
- **C10–C13 策略迭代轮**：窗口外推无漂移、影子重放归因、生产/引擎一致性审计
- **样本外漂移监控**：调参区间外验证单独累积，胜率回落自动告警

## [1.0.0] · 2026-09-06 · 首版

> 2026-08-28 以来的全部改动按主题归组；时间线：08-28 轮动+复盘合并 → 08-30 全球总览并入、
> 量化平台整体迁入 → 08-31 实时竞价新增 → 09-01 全站切换同花顺板块口径 → 09-02~03 移动端/
> 监护自愈/投机分析 → 09-04 工程加固/M_Final 备选池 → 09-05 全站双主题 → 09-06 策略 C_Final
> 收敛、备选池切换 → 首版。

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
