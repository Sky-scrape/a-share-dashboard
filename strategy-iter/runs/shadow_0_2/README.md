# 影子账本：lu 0-2% 反事实（复盘 §9 N2）

## 目的

按复盘 §9 N2 预注册 40 个验证日的 **lu 0-2%** 反事实，逐日记录真实成交与仓位，
约 40 个验证日后得到 lu 0-2% vs lu 2-5% 的 n / 均值 / t / 成交率 / 真实成交对照。

- 生产规则不变：C7 参数、买点窗、`execution_layer`、`spec_exec_t2`、OOS 基线都不动。
- 本目录只是影子账本；脚本只读 `data/recap/pool_track.json`，不做 win rate、
  继续/停止等统计判决。
- 实盘：用户按现有仓位执行；只做 0-2% 的记真实成交，做全窗的只作对照。

## 三档判定（复盘 §9 N2 原文，预注册，不另行自创）

> 第 20 日只体检；40 日或 lu 0-2% ≥10 笔：n≥10 且均值 ≥0、2-5% <0 → 收窄；
> n≥8 且均值 <0 → 停止；其余（8-9 笔正，或 2-5% 不亏）→ 延到 60 日，仍不满足
> 收窄 → 停止；n<8 且完整率 ≥80% → 延到 60 日，仍 <8 → 停止；手工账完整率
> <80% → 改月度导出。

本目录不实现判定逻辑，也不提前解释结果；到点按上面的预注册规则执行。

## 字段（13 列）

表头必须严格为：

```
date,code,name,group,open,in_window,fill,fill_price,size,fee,exit,net,note
```

| 列 | 来源 | 说明 |
|---|---|---|
| `date` | 脚本 | **验证日 T（实际买入日）**，取 `pool_track.validations` 的键，不是 `row["date"]`（那是池日 T-1） |
| `code` | 脚本 | 股票代码 |
| `name` | 脚本 | 股票名称；**相对 §9 N2 的 12 列新增**，只为人工核对，不参与判定 |
| `group` | 脚本 | 固定 `lu` |
| `open` | 脚本 | T+1 开盘涨幅（%），两位小数；只收 `0 <= open <= 2` |
| `in_window` | 脚本 | 固定 `true`（预注册窗口） |
| `fill` | 脚本预填 `unknown`，人工回填 | 是否成交（约定 `yes` / `no`；不确定先留 `unknown`） |
| `fill_price` | 人工 | 实际成交价 |
| `size` | 人工 | 实际成交数量 |
| `fee` | 人工 | 实际费用 |
| `exit` | 人工 | 实际离场价/离场口径 |
| `net` | 人工 | 实际净收益（%）；未成交留空 |
| `note` | 脚本生成，人工可改 | `pool=<池日>;ref_exec_t2=<exec_t2.net 或 NA>`；池日 T-1 只在这里出现 |

§9 N2 原 12 列为 `date, code, group, open, in_window, fill, fill_price, size, fee,
exit, net, note`；本表加 `name` 后为 13 列。脚本生成的 `note` 只作对照，人工可在
后面追加备注，但不要把 `pool=` 前缀删掉（否则无法回查池日）。

## 每日 10 分钟流程

1. 收盘后运行脚本（首日带 `--since`，之后可省略）；先看输出的新增行数。
2. 打开 `ledger.csv`，对 `fill=unknown` 的行按**真实成交**回填：`fill` 改
   `yes` / `no`，成交的再填 `fill_price`、`size`、`fee`、`exit`、`net`。
   `fill` 由脚本预填 `unknown`，这 6 列（fill / fill_price / size / fee / exit /
   net）在成交后手工回填；未成交只改 `fill=no`，其余留空。
3. 用 `note` 里的 `ref_exec_t2` 与实际/手算对照；有差异写进 `note`，不要改脚本。
4. 检查手工列完整率；完整率跌破 80% 时按 §9 N2 改月度券商导出，不假装每日手工账。

## 命令

```bat
:: 首日：必须显式给预注册起点（也接受 ISO：--since 2026-11-22）
.venv-ci\Scripts\python.exe strategy-iter\scripts\shadow_0_2_ledger.py --since 20261122

:: 重跑：只补新 (date, code)，已有行（含手工列）逐字段原样保留
.venv-ci\Scripts\python.exe strategy-iter\scripts\shadow_0_2_ledger.py

:: 预览：只打印，不写盘
.venv-ci\Scripts\python.exe strategy-iter\scripts\shadow_0_2_ledger.py --dry-run
```

- 账本不存在、或只有表头（尚未写入首行）时，必须带 `--since`；否则脚本打印
  「首次建账必须显式指定预注册起点」并以 2 退出，不创建/不修改文件。
- 首日即使没有候选（账本只有表头），下一次仍要带 `--since`（同一个，或不早于
  前一次的起点），直到写入第一行；这是为了防止 12 条历史 c7 样本在建账后回插。
- 账本一旦有行，脚本以最早 `date` 为 floor，**永不回插更早的候选**；`--since`
  可省略，也可继续带着作为额外下限。
- 默认写入 `strategy-iter/runs/shadow_0_2/ledger.csv`；`--track` / `--ledger`
  仅供测试与排障，不改默认路径。
- 写盘为同目录临时文件 + `os.replace` 原子替换，编码 `utf-8-sig`（Excel 直接打开）。

## 首日自检（必须先通过）

按 §9 N2：首日先用一条已成交的 lu 0-2% pick 手算，与 `exec_t2.net` 差
**<0.01pp** 才算通过。手算口径：T+1 开盘买、T+2 开盘卖、双边 0.15%。不通过先
停下查口径，不要开始记手工账。

## 只读声明

- 脚本只读生产侧 `data/recap/pool_track.json` 与自身 `ledger.csv`（补账），只写
  `strategy-iter/runs/shadow_0_2/ledger.csv`。
- 不写 `pool_track` / `spec_exec_t2` / `execution_layer` / 历史 `data/recap`，
  不改 C7 参数、买点窗、OOS 基线，不接告警/复审/面板。
- 不代填真实成交，不做统计判决；仓位上限由用户先写死，`size` 是手工填的实际
  成交数量，脚本不代填。
