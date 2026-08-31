# 量化平台 · 演进日志（changelog）

> 从 docs/design.md 拆出的按日期记录（design.md 只保留架构/口径/契约/路线图）。
> 新条目追加在本文件顶部区域之后按时间倒序阅读。

---

## 2026-08-30 · 设计调整第二批（Tier 1-3 全量落地）

#### 引擎与账户（地基）
- **交易日/时间戳解耦**：BarPanel 不再无条件 normalize（日内数据旧版会被静默去重成每日一根！）；
  检测 freq=intraday 时引擎**硬拒**（撮合价格锚点仍是日线假设，P2 做 ExecutionModel）；
  T+1 解锁改为仅「日历日变更」触发，不再每 bar 解锁。
- **挂单资金预留**：submit_order 用投影现金 = 现金 − pending 买单预估占用 + pending 卖单
  折价预期回款（复用 cash_demand_pct 做折扣），同日先卖后买的组合调仓不再被旧现金额卡死、
  多标的满仓买单不再全部通过后在成交阶段静默拒单。真实成交仍由 can_apply 兜底。
- **死配置全部实装**：`order_valid_bars`（cancelled 挂单在有效期内跨 bar 续挂，默认 1=旧口径）、
  `cash_demand_pct`（买入预估缓冲）、`record_orders`（False 跳过订单明细）、`t_plus_1`
  （真实开关，False 时当日买入即可卖）、`allow_short_selling=True` 显式 NotImplementedError
  （会计层 long-only，防静默错账）。新增测试锁「配置字段必须有消费方」。
- **tick 舍入唯一实现**：matching 删掉自带银行家舍入副本，统一 Contract.round_to_tick
  （half-up）；涨停价与成交价 0.005 边界不再两套口径。
- **warmup_bars**：数据窗/评估窗分离——预热段策略真实运行建立仓位，净值/成交/指标
  只统计 [start,end]。walk-forward 每折自动按网格最大数值参数推预热长度。
  ⚠️ 实现过程被「未来数据不改已评窗口 OOS」性质测试抓到过一个前视 bug（warmup 右端
  未截断），已修复并保留该测试作为常驻锁。

#### 研究诚实性（口径修正，历史结论需重跑）
- **因子工作台**：`_fwd_ret` 默认口径从「T 收盘起算」改为 **t1_open**（T+1 开盘建仓 →
  T+1+k 开盘离场），与引擎 next_open 纪律闭环；旧口径含不可交易的隔夜段，分层净值/IC
  系统性虚高。**此前所有因子分层结论请重跑**。entry 参数可切回 t_close 对照。
- **walk_forward**：新增 warmup（消除长回看参数在窗口头部被饿死、折首段强制空仓拍平）、
  `make_folds` 拒绝 step<test（测试窗重叠污染拼接）、OOS 拼接改用 equity_full（首日收益
  相对预热末权益而非初始资金的假收益）、每折新增「衰减率」列、相邻折参数迁移统计、
  过拟合裁决结构化为 `verdict` dict（阈值 decay_warn 参数化），summary 只做渲染。
- **统一 runner**（`research/runner.py`）：grid / WF 内层 / compare / UI 参数邻域 / 成本压测
  共用一个 `run_variants`（错误行、指标列、lite 引擎模式）；`make_backtest_config` 成为
  UI 侧栏与 CLI 的唯一配置构造点（CLI 补齐成本/滑点/参与率参数）；`LOWER_IS_BETTER` 单一来源。
- 引擎 `lite=True`：批量研究只产净值+指标，跳过 T×S 持仓/权重/明细表。

#### 数据可信度
- **manifest**：hithink 三类导出（个股/ETF/指数）落盘同时写目录级 `_manifest.json`
  （adjust 口径/源/行数/日期范围/导出时间/错价数）；loader 读入并校验 expect_adjust，
  不一致 → 页面红条告警（涨跌停依赖 pre_close 口径，混入 raw 会整体错位且静默）。
- **前缀单一来源**：场内基金段 `FUND_PREFIXES`（含 53）收敛 contract/hithink 三处手抄元组，
  修复 530xxx 被误路由到个股库后静默缺失。
- ETF 分红口径、除息日投票探测、错价三档等原 §4.37 内容见下方迁移段（口径未变）。

#### 工坊与沙箱
- **kind=code 存档默认拒绝回放**（分享 JSON=分享可执行代码）：网页需策略库页显式勾选
  「允许执行代码存档」，CLI 需 `--yes-run-code`；沙箱 exec 前过 AST 黑名单
  （`__class__/__mro__/__subclasses__/__globals__` 等逃逸链直接拒绝并带行号）；
  白名单去掉 `time`；「超时由前端刷新兜底」的假安全感注释改为实话。
  ⚠️ 每日 17:30 盘后信号自动化：若未来存档了 code 类策略，默认会被闸拒绝并如实记录，
  需要用户知情后给自动化加 --yes-run-code。
- **spec 单一事实源**：`validate_spec`/`operand_to_widget_keys`/`spec_to_form_state` 收进
  rule_based（可单测），GenericRuleStrategy 未知键 raise（不再 **_ignored 静默吞），
  UI 只做 session_state 搬运；表单↔spec 回填首次获得测试覆盖。
- **web 拆页**：研究与报告四子页（参数研究/因子/情绪/报告）拆到 `web/page_labrep.py`
  （显式 ctx 传参，不再依赖巨石文件全局泄漏）；主文件 1228 → ~690 行；tab_report 无结果
  的 st.stop() 改为函数 return（st.stop 会杀整个 app 的教训已由 AST 测试锁死全仓禁再犯）；
  get_panel 缓存 cache_data → cache_resource（只读消费，砍每轮整面板深拷贝）。
- **落盘路径单一来源** `quant_sim/paths.py`：store/ledger/signals 一律按项目根解析，
  换 cwd 启动不再「丢存档」；台账读取不再带建目录副作用。
- **GBK 控制台**：quant_sim 导入时对 stdout/stderr reconfigure(errors="replace")，
  CLI emoji 不再炸 UnicodeEncodeError（demo 恢复可用）。

#### 报告层与文档
- 显示口径收敛：`formatter.display_value` 唯一实现，performance._display 与
  exporter._card_value 全部委托（三份互漂的 if-else 终结）；HTML 模板外移
  `report/templates/report.html`；报告注入的用户文本（标题/备注/键名）统一 HTML 转义；
  exporter 首次获得测试（三格式落盘、无未填充 token、NaN 不崩）。
- design.md 与 changelog 分离（本文件）；README 同步；恒真测试断言清除。

#### 性能（实测）
- BarsView 懒构造（主循环每日只为查询标的建 Bar）+ limit_pct 预编译/memoize +
  close_mat 向量化 + lite 结果：1826 交易日 × 500 标的单次回测 **~12.3s → ~1.5-2.3s（约 6×）**；
  16 组合网格（500 标的）~13s。
- 未做（记录）：网格多进程（factory 多为 lambda 不可 pickle，收益/风险不匹配）；
  分钟线 ExecutionModel（引擎已硬拦，属 M3 前置工程）。

---

## 2026-08-29 之前按日期堆积在 design.md §4.35–4.39 的记录（原文迁移）

### 4.35 网页策略工坊（`web/streamlit_app.py`，五标签页）

数据（侧栏）+ 回测参数（侧栏）+ ①可视化搭建 ②在线写代码 ③内置策略 ④参数研究（网格/WF）⑤回测报告。
规则表单→JSON spec→`GenericRuleStrategy`；也可把导出的等价代码粘到②继续演化，表单与代码两条路径共用同一引擎，无隐藏分支。

---

### 4.36 策略存档 / 信号 / 归因 / 对比（M1 进阶）

* `strategies/store.py`：三类策略（rule JSON spec / code 源码 / builtin family+params）
  统一存 `strategies_store/*.json`；`build_saved()` 逆向重建实例。文件即分享格式。
* `tools/signals.py` + `BacktestConfig.liquidate_on_end`：信号模式的基石。回测到最新
  交易日不平仓，末日 `status=pending` 订单即明日开盘执行清单；CLI 写 Markdown 日报。
* `metrics/attribution.py`：月度收益矩阵（年×月+全年）、持仓周期分桶胜率、
  成本侵蚀（佣金/印花/过户/滑点合计占毛利）、盈亏榜单/按标的汇总。
* `research/compare.py`：多策略独立回测→归一净值叠加、日收益等权组合（每日再平衡口径，
  非共享账户撮合）、相关性矩阵。

### 4.37 PM 评审后的信任/沉淀/防自欺/台账包

* **ETF 分红口径**（§4.36 计划的升级）：实测 `fund.history` 统一返回含派息再投资
  的复权序列（510880 两个 ~4.6% 应然缺口日、510500 四除息日三无坑）。盲复权会造
  假涨幅，故 `apply_fund_dividend_adjust` 内置**除息日投票探测**（只计应然缺口
  ≥0.8% 的日子，接近零→复权票，接近应然坑→未复权票），未复权票多数才补复权。
* **错价三档**：keep（默认，仅告警）/ interpolate（几何中点插值+量额置 0，撮合层
  视同停牌）/ drop；`OUTLIER_STATS` 透出到页面黄色警示。
* **信号时效语义**：数据陈度 >5 天红条（先 sync），>0 天黄条标注基准日与下一交易日。
* **侧栏记忆**：全部配置存 `data/ui_prefs.json`；**回测日志** `results/backtest_log.jsonl`。
* **存档元数据**：version 覆盖自增、created 保留、列表展示版本/备注。
* **研究→资产**：网格最优参数一键存为 builtin 策略（含 symbol/universe）。
* **防自欺**：`research/sensitivity.py` 整数参数 ±20% 邻域扰动（带 entry/exit 路径
  标签去歧义）；报告页双倍成本压测（佣金/印花/最低佣/滑点×2，盈转亏即红）。
* **轻台账** `tools/ledger.py`：信号页逐单打勾记实际成交价 → 移动加权成本持仓、
  市值浮盈（缺价回退成本）、卖超截断告警、与策略目标（期末+待执行）的差额调仓单。

### 4.38 夜盘 UI 修复与一键启动（8/29）

* **theme.py 的 CSS 教训**：宽泛选择器 `[class*="st-"]` 会误伤 Streamlit 的
  emotion 类名元素，把 Material Symbols 图标字体一起盖掉 → 图标退化成
  `arrow_right` 裸文本；全局字体覆盖只碰 `html, body`，图标字体另有兜底规则。
  Google Fonts `@import` 在国内网络会阻塞渲染，等宽数字用系统栈
  （Cascadia/Consolas）。藏顶栏 chrome 必须 `display:none`（`visibility` 留幽灵
  占位导致错位）；1.62 的 Deploy 按钮实测选择器是 `[data-testid="stAppDeployButton"]`。
* **题头条遮挡**：主区 `padding-top` 不能小于悬浮工具栏高度（现 4.6rem），
  否则行情屏题头上移被盖；工具栏自身加深色不透明底防透视串扰。
* **一键启动**：`tools/launch_workshop.ps1`（探端口 8602 → 没起就静默拉起分离
  streamlit → 探到 HTTP 200 后开浏览器）+ 同目录 `launch_workshop.vbs`（无黑窗
  双击入口）+ 桌面快捷方式「量化策略工坊.lnk」（图标 `tools/workshop.ico`）。
* **Windows 坑**：PowerShell 5.1 按 ANSI(GBK) 解析无 BOM 的 UTF-8 .ps1/.vbs，
  中文注释会吞换行炸语法——启动器脚本一律纯 ASCII。

### 4.39 第一梯队研究面：因子工作台 / 情绪面板 / K线复盘 / 自动信号（8/29）

* **因子工作台** `research/factor.py`：兡 11 个量价因子（动量/反转/波动/胜率/量比/振幅/
  距新高/开盘溢价/彩票偏好代理…）+ 受限表达式组合（无 builtins 的 eval，只允许
  因子名/rank/四则运算）。逐日 Rank IC（Spearman，NaN 感知）、ICIR/t 值、Q1..Qn
  分层净值+多空线、多持有期 IC 衰减、因子自相关（≈换手代理，判费后可行性）、
  因子间横截面相关热力图。停牌日（量=0）不参与截面；截面样本 < min_cross 的
  日子直接剔除并在 UI 告警（小样本不假装显著）；分层净值零成本理想化，UI 明标。
  UI：研究与报告 → 🧪 因子工作台。
* **情绪面板** `research/sentiment.py`：hithink special-data 涨停/炸板/跌停池逐日
  回填（--date-ms 历史可用，实测）→ `data/sentiment/raw_*.json` 缓存；炸板率、
  晋级率（今日≥2板/昨日涨停总数）、连板高度、封单 Top8 结构；趋势线挂
  今日信号旁的 🌡 市场情绪子页（页内不自动拉网，按钮手动补齐；
  `python -m quant_sim.research.sentiment --days 20` 可命令行预热）。
  修了桥接层隐藏 bug：`_cli` 的 subprocess 无 encoding 参数，GBK 默认解码 UTF-8
  中文股票名直接炸（个股行情全数字所以一直没暴露）。
* **K线成交复盘**：报告页 expander，自选复权日 K（红涨绿跌）+ 成交日 ▲/▼ 标记
  （面积按股数）、该标的 FIFO 配对已实现盈亏表；matplotlib 服务端渲染需显式
  中文字体（Microsoft YaHei）否则豆腐块。
* **盘后自动信号**：Proma 定时任务「量化平台盘后信号日报」每日 17:30（避开同机
  17:00 的看板数据任务）：交易日判定→data sync→全部存档策略 signals run→情绪
  预热→追加 `results/daily_signal_log.md`+短摘要；跨运行记忆在
  `.context/automation/daily-post-close-signal/notes.md`。
* 测试：+7（因子统计核 4、情绪解析/缓存/失败降级 3），全套 62 绿。
