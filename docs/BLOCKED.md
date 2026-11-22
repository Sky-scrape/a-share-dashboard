# BLOCKED

当前没有阻塞开发/运行的硬阻塞。以下 14 项需要用户拍板、凭据或管理员权限，暂缓：

1. **notify 凭据**：在 `.status/notify.json` 填 Server酱 sendkey / 企业微信 webhook / Telegram bot_token+chat_id 之一；配好后用 `.venv-ci\Scripts\python.exe backend\notify.py --send` 自检。本轮未配置真实凭据。
2. **StopOnIdleEnd 提权执行**：以管理员运行 `docs/repair-sessions/20261122_fix-task-idle.ps1`；当前 `rotation-intraday-fetch` 与 `arecap-usclose-fetch` 仍为 `StopOnIdleEnd=true`。
3. **C7 去留/临时降仓**：策略判决，需拍板；本轮不改 C7 参数、买点窗、OOS 基线。
4. **lu 0-2% 正式收窄**：等 40 个验证日影子账本按复盘 §9 N2 三档判定（约 2027-01 下旬）。
5. **exec severe 接进推送/复审**：等校准门；当前只下面板，不接告警。
6. **quant / exe 去留**：他机使用与面试用途未确认；不删、不摘 CI、不重发 exe。
7. **17:05 global 拆出/改时点**：需先跑 3 个工作日 ×50 只 probe，属产品决定。
8. **C8 版本前缀协议重构**：会清零样本，需拍板。
9. **删除未跟踪 / LEGACY / .bak / 乱码文档**：需审计确认无人依赖；本轮 LEGACY 清理已改 `--prune` 默认关闭。
10. **影子账本手工填单**：`fill/fill_price/size/fee/exit/net` 由用户每日按真实成交填；首日手算与 `exec_t2.net` 差 `<0.01pp` 才算通过。
11. **3 个工作日 ×50 只 global CN 抽样**：运行 `.venv-ci\Scripts\python.exe tools\probe_global_cn.py --limit 50`，人工判读限流/时点/改版。
12. **`data/rotation/boards_legacy_eastmoney.json` 旧副本**：`config.BOARDS_FILE` 已改指 `boards_legacy.json`，旧文件合并/改名/删除需确认。
13. **notify.degraded 前端横幅与 health 告警接线**：本轮只加字段/日志/ERROR 级，前端横幅未做。
14. **影子账本 `pool_date` 列**：当前 13 列（N2 12 列 + `name`），池日只写在 `note`；若按池日筛选需改表头，已有账本会失效。
