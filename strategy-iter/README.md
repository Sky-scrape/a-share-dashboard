# strategy-iter · 策略自迭代研究库

策略自迭代的完整工作区（C1→C20 轮），产出主线策略 **C_Final** 与每日「明日交易备选池」。
生产主线是 **C7**（固化口径：入选门槛 70 + 非涨停组市场量能闸门），研究已推进到 C20；
C16~C20 的七轮负结果与三因子准入池见 `runs/` 与 `reports/rounds_log.md`。
方法论全文见 [自动选股与策略自迭代系统.md](自动选股与策略自迭代系统.md)；
执行层口径单一来源在 `backend/execution_layer.py`（研究脚本与生产共用，不许复制）。

## 目录

```
strategy-iter/
├── engine/     可复用迭代引擎（rules/select/precompute/run_round/validate，规则版本冻结在 R.VERSIONS）
├── scripts/    76 个研究脚本 + common.py（hithink 抓取公共库：C6~C20 抓取/回测/消融/离场/影子账本；核心口径抽成纯函数，可被 tests 引用）
├── runs/       每轮迭代产物窗口（窗口外推/消融/影子重放留痕；含 shadow_0_2/ 影子账本）
├── reports/    轮次日志 rounds_log.md + 阶段性总结 + 抓取日志
├── data/       研究用原始数据（parquet，gitignored，可由 scripts 重建）
└── raw/        原始抓取层（gitignored）
```

## runs/ 保留规则（2026-09-27 复核）

- **runs/ 的产物文件是 git 跟踪的**（仅 data/ 与 raw/ 不入库）——每一轮的报告、
  曲线、验证表都随仓库版本化，git 历史本身就是归档，**不做 zip 二次归档、不删任何轮次**：
  留痕是防过拟合审计的证据链（哪轮改了什么、样本外表现如何），
  `reports/rounds_log.md` 是各轮结论的索引；
- 磁盘增长的真实来源是 `data/`（134M，可由 scripts 重建）与 `raw/`（42M，原始抓取层），
  二者不入库、由 `backend/backup.py` 滚动 zip 兜底；如需清理只动这两处；
- 特例：`runs/concept_round3_C3/` 是 C_Final 资金曲线的证据产物，受
  `tests/smoke.py` 守护（缺文件即红），任何清理操作不得触碰。

## 与生产的关系

- 引擎口径改动必须走「先研究轮验证 → 再同步生产」的顺序；生产侧消费点只有
  `backend/recap/spec_pool.py`（备选池）与 `spec_validate.py`（样本外漂移监控）。
- **生产 C7 对拍**：`tests/test_c7_parity.py` 把生产 `spec_pool.py` 的 C7 版本、
  隔夜美股闸门、NLU 量能闸门与 lu/nlu 默认/状态文件门槛对到本目录
  `engine/rules.py`；改任一侧口径必须同步，否则测试变红。
- `scripts/mf_mono_backtest.py`、`diaoling_lianban_backtest.py` 等对照回测的执行
  近似与统计弱点在各脚本头部显式披露（如「单吊是单路径模拟」）——引用数字时先读。

## 影子账本（runs/shadow_0_2/）

按复盘 §9 N2 **预注册**的 lu 0-2% 反事实账本，入口
`scripts/shadow_0_2_ledger.py`，说明/三档判定原文/每日流程见
[runs/shadow_0_2/README.md](runs/shadow_0_2/README.md)。

- `date` 是**验证日 T（实际买入日）**，取 `pool_track.validations` 的键；池日 T-1
  只写在 `note` 的 `pool=` 前缀里。
- 首次建账（账本不存在或只有表头）必须带 `--since YYYYMMDD`；账本一旦有行，
  脚本以最早 `date` 为 floor，永不回插更早候选。`fill` 由脚本预填 `unknown`，
  成交后人工回填；脚本不代填成交、不做统计判决。
