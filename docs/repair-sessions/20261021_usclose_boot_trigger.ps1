# 2026-10-21 完成链「开机即补跑」批：给 arecap-usclose-fetch 增加两条 BootTrigger。
#
# 背景（实况，2026-10-21 核查）：
#   - 本机夜间被「手动关机」（S5）——10-21 00:01 User32 1074 关闭电源、00:01:30 OS 关闭，
#     直到 07:24:32 才冷启动；10-20 03:58 关机 / 07:28 开机；10-19 01:21 关机 / 07:12 开机，
#     即 04:05 触发「必然」被错过。
#   - powercfg /a：本机无休眠（未启用）、无 S1/S2/S3，唯一睡眠形态是 S0 低电量待机
#     （网络已断开连接）；快速启动/休眠均不可用。
#   - powercfg /lastwake：唤醒历史记录计数 = 0（从未有定时唤醒成功过）。
#   → WakeToRun 无法把机器从「关机」唤醒；唯一可行的兜底是「开机后 StartWhenAvailable
#     补跑」，而该补跑实测要等 7~10 分钟（10-20 07:28 开机→07:35 触发；10-21 07:24 开机
#     →07:34 触发）。
#
#   本脚本写两条 BootTrigger：
#     1) Delay=PT1M  —— 开机后 1 分钟就跑，把「开机后出池」从 7~10 分钟压到 1~2 分钟；
#     2) Delay=PT15M —— 二次机会：开机瞬间网络未就绪会让第 1 次抓取失败（链内已配
#        US_FETCH_RETRY_S=90s 重试），若仍失败则本次跳过终版结算、该日池会顺延到下一次链；
#        15 分钟后网络必已就绪，这条触发器再跑一次把池补出（缺口兜底此时也仍有效）。
#   两条与既有 04:05 日历触发并存；MultipleInstancesPolicy=IgnoreNew 天然防重。
#   注意：04:05 日历触发保留——哪天机器夜里没关，仍按原时序在收盘后跑。
#
# 任务原有触发器/动作/其余设置一律 Export→Register 原样保留（含 2026-10-21 已改的动作
# wscript.exe _hidden_runner.vbs）。
# 幂等：重复运行只会留下「恰好两条」BootTrigger（先清空既有 BootTrigger 再插）。
# 需管理员权限（UAC）：改计划任务在本机是 admin-only（2026-10-16 同因）。
$ErrorActionPreference = 'Stop'
$root = 'D:\AsusDownload\Desktop\A'
$log = Join-Path $root '.status\logs\fix_usclose_boot.log'
$bak = Join-Path $root '.status\task-backup\arecap-usclose-fetch-boot.xml'
$task = 'arecap-usclose-fetch'

"==== fix_usclose_boot start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8

try {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
              ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Not elevated: 请右键「以管理员运行」本脚本（UAC -> 是）。'
    }

    $xml = Export-ScheduledTask -TaskName $task
    New-Item -ItemType Directory -Force -Path (Split-Path $bak) | Out-Null
    $xml | Out-File $bak -Encoding utf8

    # 幂等：清空既有 BootTrigger 后插入两条（1 分钟立即补跑 + 15 分钟二次机会）
    $xml = $xml -replace '(?s)\s*<BootTrigger>.*?</BootTrigger>', ''
    $xml = $xml -replace '<Triggers>', ("<Triggers>`r`n" +
        "    <BootTrigger><Delay>PT1M</Delay></BootTrigger>`r`n" +
        "    <BootTrigger><Delay>PT15M</Delay></BootTrigger>")
    Register-ScheduledTask -TaskName $task -Xml $xml -Force | Out-Null

    # 回读校验：必须恰好两条 BootTrigger（PT1M / PT15M），否则视为未生效
    $t = Get-ScheduledTask -TaskName $task
    $boots = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -match 'Boot' })
    $dels = @($boots | ForEach-Object { $_.Delay })
    if ($boots.Count -ne 2 -or -not ($dels -contains 'PT1M') -or -not ($dels -contains 'PT15M')) {
        throw "回读异常：BootTrigger 数=$($boots.Count) delays=$($dels -join ',')"
    }

    $trg = (($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', ')
    "OK 触发器=$trg ；BootDelays=$($dels -join ',')" | Out-File $log -Append -Encoding utf8
    "OK WakeToRun=$($t.Settings.WakeToRun) StartWhenAvailable=$($t.Settings.StartWhenAvailable) MultipleInstances=$($t.Settings.MultipleInstances) State=$($t.State)" | Out-File $log -Append -Encoding utf8
    "OK 动作=$($t.Actions[0].Execute) $($t.Actions[0].Arguments)" | Out-File $log -Append -Encoding utf8
}
catch {
    "FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

"==== done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
Write-Host "done: see $log"