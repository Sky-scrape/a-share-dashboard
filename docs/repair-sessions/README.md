# docs/repair-sessions · 事故修复脚本与证据

本目录只放「一次性事故修复」脚本与最小证据，不是日常工具目录。新增脚本先在
本文件登记，再由管理员手动执行并回读；脚本默认不自动运行。

## 保留规则

1. **事故修复脚本与必要证据保留**：`.ps1` / `.bat` / 一次性 `.py`，以及任务 XML
   备份、`powercfg` 输出等小体积文本证据，是审计与回滚依据，不随轮次清理。
2. **一次性 `.bak` 超过 30 天，确认无回滚需求后清理**：本目录现有
   `20260922.json.bak-pre-dt-repair`（2026-09-23）与
   `pool_track.json.bak-20261016`（2026-10-16）均已超过 30 天，**待确认后再删**，
   不要自动清理；删前确认对应数据已由 git 或后续快照覆盖。
3. **docs/legacy/ 只留代码留痕，不再维护**：合并前的旧服务/脚本留在那里仅作
   历史参考；新的一次性修复脚本不要放 `docs/legacy/`，放本目录并登记。
4. **不存密钥**：脚本只写路径与参数，凭据继续留在 `.status/notify.json` 等
   gitignored 文件里。
5. `.status/task-backup/*.xml` 是本机回滚证据（gitignored）；换机/重装前先备份
   该目录。

## 修复脚本索引

| 脚本 | 用途 | 需管理员 | 是否已执行 | 回退方式 |
| --- | --- | --- | --- | --- |
| `20261016_fix_recap_wake_and_tasklog.ps1`（配套 `以管理员运行修复.bat`） | 给 `arecap-daily-fetch` 开 `WakeToRun`；启用任务计划程序 Operational 日志 | 是（改计划任务） | 是。证据：`.status/task-backup/arecap-daily-fetch-wake.xml`（2026-10-16 10:47）与提交 598012c；`.status/logs/fix_recap_wake.log` 当前已不存在 | 用改前 XML（先核对 `.status/task-backup/arecap-daily-fetch*.xml` 内容）`Register-ScheduledTask -Xml -Force` 重新注册 |
| `20261021_usclose_boot_trigger.ps1`（配套 `以管理员运行修复-20261021开机触发器.bat`） | 给 `arecap-usclose-fetch` 加两条 BootTrigger（PT1M / PT15M），不改 04:05 日历触发 | 是（改计划任务） | 是。证据：`.status/task-backup/arecap-usclose-fetch-boot.xml`（改前备份，2026-10-21 08:26）；当前任务 XML 确有两条 BootTrigger；提交 5144a8c 记录回读 | 用该改前 XML `Register-ScheduledTask -TaskName arecap-usclose-fetch -Xml ... -Force` 覆盖 |
| `20261122_fix-task-idle.ps1` | 就地修 `rotation-intraday-fetch` 与 `arecap-usclose-fetch`：`StopOnIdleEnd=false`（缺失 `IdleSettings` 则补），导出/注册/回读；只改这一项 | 是（改计划任务） | **否**。2026-11-22 只交付脚本；`schtasks /query /XML` 回读两任务当前仍为 `StopOnIdleEnd=true` | 首次运行会把改前 XML 备份到 `.status/task-backup/<task>-idle.xml`（已存在不覆盖）；用该 XML `Register-ScheduledTask -Xml -Force` 回退 |
| `rebuild-intraday-task.ps1` | 重建 `rotation-intraday-fetch`（每日 09:25 + PT5M/PT5H45M 重复触发 + 不看电池/补跑/8h 上限）；2026-11-22 起带 `-DontStopOnIdleEnd` | 是（S4U 注册） | 是（2026-09-03 紧急重建）。证据：当前任务触发器 `2026-09-03T09:25`、`PT5M/PT5H45M` 与脚本一致；无 XML 备份 | 脚本不备份：回退前先 `Export-ScheduledTask` 留档，再按旧参数重建；不要直接 `Unregister` 后无备份重来 |
| `fix_task_windows.ps1` | 把 4 个任务的登录类型从 `InteractiveToken` 改为 S4U（不弹终端窗） | 是（改计划任务） | 是。证据：`rotation-intraday-fetch` 当前 `LogonType=S4U`；`.status/logs/fix_task_windows.log` 当前不存在 | 改回 `InteractiveToken`（会恢复弹窗），需管理员；改前先导出 XML |
| `repair_0915_modules.py` | 20260915 快照五模块合并式回填（breadth/concepts/extra/hot_stock/etf；不可回补字段置空并标注） | 否（只写 data/） | 是（脚本头注明“跑完即删”，文件仍留存作证据） | 用对应日期 `.bak` 快照恢复；不可回补部分保持空值 |
| `repair_boards_history.py` | 修复 `providers.boards` 的 `.TI.TI` 双后缀导致 20260910~20260918 行业 5 日线只剩 1 天 | 否（只写 data/） | 是（脚本头记录实扫 174 份快照后回填） | 用 `.bak` 快照回退；重跑脚本前先备份 |
| `repair_pool_payload.py` | 修复 20260914~17 等快照内嵌的备选池验证/优化器旧载荷 | 否（只写 data/） | 是（脚本头记录实扫后定位） | 用 `pool_track.json.bak-*` 与对应快照 `.bak` 回退 |

## 证据文件

- `evts.ps1`：只读导出任务计划程序 Operational 日志中 `rotation-intraday` 事件。
- `params.txt` / `ti.txt` / `concepts_0915.json`：2026-09 事故定位时的参数与现场样本。
- `*.bak-*`：一次性数据备份，按上面第 2 条保留/清理。

## 新增脚本约定

- 先在本文件登记用途、是否需管理员、回退方式；脚本头写清「只改什么、不做什么」。
- 默认「交付不执行」，由管理员手动运行；结束时打印回读命令。
- 计划任务类脚本必须：先 `Export-ScheduledTask` 备份 → 只改目标项 →
  `Register-ScheduledTask -Xml -Force` → 回读校验；幂等，可重复运行。
- PowerShell 5.1 读无 BOM 的 UTF-8 `.ps1` 会按 ANSI 解析：脚本一律 UTF-8 带 BOM
  或纯 ASCII。
