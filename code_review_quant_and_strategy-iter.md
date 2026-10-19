# 代码审查报告 · quant（quant_sim 量化引擎）+ strategy-iter（策略自迭代）

审查日期：2026-10-19
审查者：team-code-reviewer

---

## 0. 审查范围与证据边界（先声明，避免误读）

**本次逐行读取、结论有直接代码证据的模块：**
- `quant/quant_sim/report/exporter.py`
- `quant/quant_sim/tools/signals.py`
- `quant/quant_sim/research/runner.py`、`research/grid_walkforward.py`
- `quant/quant_sim/strategies/store.py`、`strategies/sandbox.py`（经 grep 命中）
- `quant/tests/test_custom_strategies.py`
- `quant/quant_sim/data/loader.py`、`data/manifest.py`（经 grep 命中关键行为链）
- `backend/quant/quant_api.py`、`backend/quant/run_job.py`、`web/quant/app.js`（经 grep 命中调用链）
- `strategy-iter/engine/precompute.py`、`engine/rules.py`（部分）
- `strategy-iter/scripts/c15_exec_tplus.py`、`scripts/backtest_capital.py`

**因上下文压缩（大附件超限被移除）未能即时取回完整内容的模块**（本报告不对其臆造逐行结论，仅在其有 grep 命中证据时陈述）：
`quant_sim/core/{matching,cost,risk,account,engine,types,config,contract,fsutil}.py`、
`data/adjustment.py`、`metrics/performance.py`、`research/factor.py`、`strategies/rule_based.py`、
`strategy-iter/engine/{select,validate,run_round}.py`、`scripts/c15_common.py` 等。
→ 建议对上述模块安排第二轮专项复核（见文末「遗留待复核项」）。

---

## 1. 严重问题（Critical）

### C-1 执行口径含未来不可实现的 T+1 当日卖出，系统性高估收益
- **问题描述**：策略自迭代的生产/研究「执行近似」允许 **T+1 买入当日**按 `risk_low` 止损离场
  （`spec_validate._exec_pnl_realtime` 与 `backtest_capital.trade_pnl` 为同一实现），但 A 股
  **T+1 制度下当日买入不可卖出**，该止损在实际中不可执行，等于给每笔交易一个「盘中止损保底」，
  系统性高估收益、低估回撤。团队已用 `c15_exec_tplus.py` 量化了该偏差（诊断口径 A vs 制度口径 B/C），
  但**尚未回写修正生产诊断口径**。
- **所在文件/行号**：
  - `strategy-iter/scripts/c15_exec_tplus.py:4-18,125-127`（三套口径并列，A 明确标注"不可执行"）
  - `strategy-iter/scripts/backtest_capital.py:104,116`（`exit_pct = risk_low if 触及 else c`）
- **改进建议**：
  1. 将制度口径（B：T+1 买→T+2 开盘卖）设为**生产默认**，把现行 A 口径降级为「乐观上界参照」并显式命名；
  2. 在选股/审阅报告（`runs/*/report.md`）顶部固定展示「诊断口径 vs 制度口径」差额，作为必读警示；
  3. 至少为 A 口径输出加 `unrealistic=True` 标记，避免下游（backtest_capital、候选对比）误当可执行收益引用。

### C-2 跌停「开盘不可卖」判定写错，漏判最主要的不可卖情形（偏乐观）
- **问题描述**：`unsellable_open` 要求 **`开盘价 == 昨收`** 且 `收盘 == 跌停价` 才置位。真正的
  「T+2 开盘跌停不可卖」应是 **开盘价 ≈ 跌停价（`rpc×(1-10%)`）**。当前条件只在「平开、收跌停」
  这一少见组合下触发，会**漏掉一字/开盘即跌停**的主要情形 → 该样本仍按 `r2o`（开盘价）离场 →
  `B/C` 口径偏乐观。此外开盘判定容差 `1e-6` 与收盘判定容差 `EPS=0.005` 不一致。
- **所在文件/行号**：`strategy-iter/scripts/c15_exec_tplus.py:17-18,119-124`
- **改进建议**：
  ```python
  limit_down = round(rpc * (1 - LIMIT_PCT), 2)
  rec["unsellable_open"] = bool(
      rc and rpc and bar2["o"] is not None
      and abs(bar2["o"] - limit_down) <= EPS      # 开盘即跌停 → 无法卖出
  )
  ```
  并统一容差口径；对「开盘跌停」与「盘中跌停」分别计数。

---

## 2. 重要问题（Important）

### I-1 数据口径不一致（复权 vs 实际）不阻断引擎，仅在 UI 拼一条提示
- **问题描述**：`loader` 只把不一致写进 `panel.metadata["manifest_mismatch"]`，
  **引擎/回测不检查**；`quant_api.py` 也只是把它拼成一条普通提示字符串（非阻断）。`docs/design.md:180`
  声称"页面红条阻断提示"。后果：复权口径不符时涨跌停/停牌判定会系统性错位，而 **CLI / 研究路径
  （`tools/signals.py`、`research.runner`）完全无任何告警**，可能"静默按错口径出信号"。
- **所在文件/行号**：
  - `quant/quant_sim/data/loader.py:71,117-125`
  - `quant/quant_sim/data/manifest.py:10`（注释自称"由 UI/报告醒目告警——不静默使用"）
  - `backend/quant/quant_api.py:221-222`（仅 append 提示，不 raise）
  - `quant/quant_sim/tools/signals.py:67-78`（只查"数据新鲜度"，不查口径）
- **改进建议**：
  1. `run_backtest` 入口（或 loader 可配开关）在 `manifest_mismatch` 非空时 **默认 raise 或 warning**；
  2. `signal_report` 增加口径告警段（与新鲜度告警同位置、同醒目度）；
  3. 让"阻断"名副其实：UI 侧把红条做成**阻止提交回测/信号**，而非仅提示。

### I-2 代码策略沙箱：白名单为模块级可变全局，且逃逸面测试覆盖不足
- **问题描述**：沙箱用受限 builtins + import 白名单 + dunder 黑名单 + `exec(compile(...))`，
  文档已声明"防误不防恶、无超时"，属**可接受的设计取舍**。但：
  - `_ALLOWED_MODULES` 是**模块级可变全局**，测试直接整体替换它（`test_custom_strategies.py:115-121`）
    ——生产进程内任何能触达该名的代码路径都可扩大白名单；
  - 现有逃逸面测试只覆盖 `os/subprocess/open/exec`、无策略类、语法错误（`test_custom_strategies.py:163-173`），
    **未覆盖** `__builtins__`、`__import__`、`__reduce_ex__`、`__loader__` 等 dunder 逃逸链，
    也未覆盖 `importlib`/`pyexpat`/白名单外模块，**测试对"防逃逸"这一核心承诺无回归保护**。
- **所在文件/行号**：`strategies/sandbox.py:78,102,123`；`tests/test_custom_strategies.py:115-121,163-173`
- **改进建议**：
  1. 白名单改为**不可变**（`frozenset`）+ 构造期参数化（`build_user_strategy(code, allow_modules=...)`），
     禁止运行期改全局；
  2. 补逃逸回归用例：`().__class__.__bases__`、`__import__('os')`、`__builtins__['open']`、
     `getattr(x,'__reduce_ex__')`、`import importlib`、`().__getattribute__('__class__')`；
  3. 若产品允许，评估给 `exec` 加超时/内存上限（进程隔离或 signal/子进程），把"卡死需重启"降级为可控。

### I-3 JSON 存档反序列化：可执行源码重放 + schema 无校验 + 覆盖保存静默改写版本
- **问题描述**：`kind=code` 存档回放会**重新执行存档内源码**；默认 `allow_exec=False` 拒绝是**好闸门**，
  且属可接受取舍。但仍存在：
  - `payload` 无 schema 校验，`s.payload["code"]` 直接下标 → payload 缺字段时抛 **`KeyError`**（报错不友好）；
  - 存档文件无签名/完整性校验，任何本地进程改写 JSON 即可注入代码，加载时无来源校验；
  - 覆盖保存时读旧文件失败被 `except Exception: pass` 吞掉 → `created/version` **静默重置为 1**，
    破坏"版本可追溯（研究迭代）"的承诺。
- **所在文件/行号**：
  - `strategies/store.py:212-234`（`build_saved`，`allow_exec` 闸门与 `payload["code"]`）
  - `strategies/store.py:154-163`（版本静默重置）
  - `strategies/store.py:174-193`（`list_strategies` 静默跳过坏档）
- **改进建议**：
  1. `load_strategy` 增加 payload schema 校验，`code` 缺失给明确 `ValueError`（含文件名）；
  2. 代码存档写入 `sha256` 指纹，加载时校验并展示来源，防静默篡改；
  3. 覆盖保存读旧档失败时**显式警告**（不静默重置 version）；
  4. `list_strategies` 跳过坏档时记录计数/日志，供 UI 提示"有 N 个存档无法读取"。

### I-4 两套并行回测实现，费用/滑点常量多处硬编码 → 口径漂移
- **问题描述**：项目同时存在 (a) `quant_sim` 生产引擎（`CostConfig`：佣金/最低佣/印花税/过户费/滑点）
  与 (b) `strategy-iter/scripts` 自建近似（简单百分比扣费）。二者口径不同却常在报告里并列比较。
  且 `c15_exec_tplus.py` 第 34 行刚强调「执行层单一来源（不许复制常量）」地 import `MF_BUY`，
  紧接第 37 行却把 `FEE_MAIN = 0.15` **硬编码**（注释称与 `mf_mono_backtest.FEE` 同源）。
- **所在文件/行号**：
  - `strategy-iter/scripts/c15_exec_tplus.py:34,37,148`（`net = [v - fee ...]` 简单百分点相减）
  - `strategy-iter/scripts/backtest_capital.py:53,57-58`（此处做到了单一来源，可作正面参照）
  - `quant/quant_sim/research/runner.py:91-102`（`stress_config` 是 quant_sim 侧唯一的成本派生点，良好）
- **改进建议**：
  1. 把 `FEE`（双边成本）也收进 `execution_layer`，脚本一律 import，禁止复制；
  2. 在报告标题/表头注明口径来源（"quant_sim CostConfig" vs "脚本近似 0.15% 双边"），避免读者误比；
  3. 条件允许时让 `backtest_capital` 改为调用 quant_sim 引擎，消除双实现。

### I-5 台账与参考现价处 `except Exception: pass` 吞掉异常
- **问题描述**：信号日报中"参考现价"取值失败（含历史 AttributeError）被静默吞掉并显示 `-`；
  台账对比失败同样静默。台账可视为"增值信息"（注释已说明，**可接受**），但参考现价是**执行清单的关键字段**，
  静默吞异常会让用户在下单前失去价格发现能力且无任何提示。
- **所在文件/行号**：`quant/quant_sim/tools/signals.py:98-104`（参考现价）、`124-144`（台账）
- **改进建议**：参考现价失败改为 `logging.warning` 或直接在报告里标注"取价失败"，不要静默降级为 `-`。

### I-6 `strategy-iter/engine/rules.py` 版本常量六处内联重复
- **问题描述**：V1/V2/V3/M1/M2/M3 六个版本各自内联 `trigger_low_min=-3.0, risk_low=-5.0/-4.0,
  risk_close=-4.0/-3.5`，同一组风险位**重复 6 遍**。任一处修改都需同步 6 处，极易漏改导致版本间口径漂移。
- **所在文件/行号**：`strategy-iter/engine/rules.py:121-123,171-173,224-226,283-285,341-343,396-398`
- **改进建议**：抽 `_RISK_LU = dict(trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0)` 等公共
  模板，各版本用 `{**_RISK_LU, ...}` 增量覆盖，并加一个测试断言"各版本风险位来自公共模板"。

---

## 3. 性能热点（Important / 建议）

### P-1【重要】`precompute.build_concept_tables` 逐行 `iterrows` + 逐票 Python 循环
- **问题描述**：概念×日期表用 `set_index(...).iterrows()` 逐行建 dict（可能数万行），
  随后 `best_at/gate_at` 对池内每行再 `zip` 逐条调用（O(池行数 × 所属概念数)）。纯 Python 循环，
  在概念/池规模上会成倍放大。
- **所在文件/行号**：`strategy-iter/engine/precompute.py:73，81-114，195-205`
- **改进建议**：改用向量化——`cd` 直接用 `MultiIndex` + `map/merge` 挂接到 `pool`；
  `best_at` 用 `groupby('thscode')` 取 `idxmax` 向量化替代逐行函数调用。

### P-2【重要】`precompute` 龙虎榜窗口：每交易日全表 `isin` + `groupby`
- **问题描述**：
  ```python
  for i, d in enumerate(td):                 # 每个交易日循环
      window = td[max(0, i - 5):i]
      sub = net[net["date"].isin(window)]    # 全表过滤
      agg = sub.groupby("thscode")["net_value"].sum()   # 全表 groupby
  ```
  复杂度 O(交易日数 × 龙虎榜行数)，且每轮重建 groupby，是明确的性能热点。
- **所在文件/行号**：`strategy-iter/engine/precompute.py:170-180`
- **改进建议**：按 `date` 排序后用 `rolling`/`groupby+shift` 或先做
  `net.set_index('date').loc[window]` 的切片；或一次性 `cumsum` 按 5 日滑动窗口做差分。

### P-3【建议】死代码与无意义聚合
- **问题描述**：`bars = si.groupby("thscode")["bars"].apply(lambda s: s)` 结果被赋给局部变量 `bars`
  后**从未使用**（仅注释"keep si lookup"），却执行了一次完整 groupby+apply 开销。
  同理 `c15_exec_tplus.py:133-135` 的 `gross()` 是恒等函数（死代码）。
- **所在文件/行号**：`precompute.py:162`；`c15_exec_tplus.py:133-135`
- **改进建议**：直接删除。

### P-4【建议】walk_forward 每折整网格重跑
- **问题描述**：`walk_forward` 每折调用 `grid_search`（整网格 × 全量 data）。属研究手段（文档说明），
  且引擎对 `panel.metadata` 有切片缓存可跨 run 复用，故**风险可接受**；但折数 × 网格规模较大时耗时显著。
- **所在文件/行号**：`quant/quant_sim/research/grid_walkforward.py:209-217`
- **改进建议**：暴露 `n_jobs`/进度回调已在；可进一步复用 `_slice_cache` 或对 IS/OS 窗口预切片。
  另注意 `grid_walkforward.py:259-261`：若所有折 `daily_ret` 均为空，`pd.concat` 得空表后
  `cumprod`/`compute_metrics` 可能异常，建议加"空 OOS 序列"显式保护。

---

## 4. 建议改进（Suggestions）

- **S-1 沙箱无超时**：`exec` 用户代码无超时/内存限制，"卡死需重启"已在注释与设计文档披露
  （**可接受取舍**），但建议评估子进程隔离，把不可控降级为可控。
  `strategies/sandbox.py`（设计文档 `docs/design.md:138`）。
- **S-2 测试覆盖**：`quant/tests/test_custom_strategies.py` 已覆盖规则导出等价性、模板可跑、
  危险代码拒绝（良好）；**缺口**见 I-2（逃逸链）与"store 损坏档/缺字段"边界用例。
- **S-3 报告导出健壮性**：`report/exporter.py` 对注入模板的用户文本统一 `_esc` 转义、`DATA` 用
  `json.dumps(default=str)`（**良好实践**）；但 `_template` 用 `lru_cache(maxsize=1)` 读盘，
  改样式需重启进程，建议开发模式加文件 mtime 失效。
- **S-4 前端 allow_exec 闸门**：`web/quant/app.js:881` 通过勾选框传 `allow_exec`——闸门确实存在，
  建议在 UI 上把「允许执行代码存档」的后果文案与存档指纹一并展示，强化知情确认。
- **S-5 演示数据标注**：`exporter.py:139` 对合成数据自动加"⚠️ 合成演示数据"标注——良好，
  建议同类标注机制扩展到"口径不一致/数据陈旧"，与 I-1 联动。

---

## 5. 结论

- **阻塞级（Critical）2 条**（C-1 执行口径 T+1、C-2 跌停不可卖判定），均集中在
  `strategy-iter` 的执行近似层，且都指向**同一方向偏差：高估可执行收益**，建议优先修复。
- **重要级（Important）6 条**：口径告警不阻断（I-1）、沙箱白名单可变与逃逸测试缺口（I-2）、
  存档反序列化健壮性（I-3）、双实现费用口径漂移（I-4）、关键字段静默吞异常（I-5）、
  配置常量六处重复（I-6）。
- **性能**：热点集中在 `precompute.py`（iterrows / 每交易日全表扫描），建议向量化。
- **可接受的设计取舍**（非缺陷，已文档化）：沙箱"防误不防恶、无超时"、网格/WF 计算量、
  研究脚本与生产引擎并存的近似口径——但**都必须显式标注口径来源**，避免读者误当可执行结论。

### 遗留待复核项（因上下文压缩未取回内容，非"未发现问题"）
请安排第二轮专项复核以下模块，重点：
1. `quant_sim/core/matching.py`：涨跌停/停牌撮合是否**用未复权价**判封板（`c15_exec_tplus.py:119-120`
   已指出"复权价无法判跌停"，需确认 core 是否一致）；
2. `core/cost.py`：印花税/过户费/最低佣金的计费方向与四舍五入；
3. `core/risk.py`、`core/account.py`：T+1 可卖数量冻结、风控触发时机；
4. `core/engine.py`：pending 单资金投影一致性、`_slice_cache` 跨 run 复用正确性；
5. `metrics/performance.py`：年化交易日基数（244）与卡玛/夏普无风险利率口径；
6. `research/factor.py`：表达式求值器（`eval` 面）的安全与性能；
7. `strategy-iter/engine/{select,validate,run_round}.py`：硬断言（US 覆盖率 / concept_map 新鲜度）
   之外的分支与错误处理。
