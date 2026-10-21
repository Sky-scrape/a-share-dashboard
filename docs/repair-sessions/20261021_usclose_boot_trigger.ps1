# 2026-10-21 完成链「开机即补跑」批：给 arecap-usclose-fetch 增加 BootTrigger。
#
# 背景（实况，2026-10-21 核查）：
#   - 本机夜间被「手动关机」（S5）——10-21 00:01 User32 1074 关闭电源、00:01:30 OS 关闭，
#     直到 07:24:32 才冷启动；此前 10-20 03:58 关机 / 07:28 开机，10-19 01:21 关机 /
#     07:12 开机——即 04:05 触发「必然」被错过。
#   - powercfg /a：本机无休眠（未启用）、无 S1/S2/S3，唯一睡眠形态是 S0 低电量待机
#     （网络已断开连接）；快速启动/休眠均不可用。
#   - powercfg /lastwake：唤醒历史记录计数 = 0（从未有定时唤醒成功过）。
#   → WakeToRun 无法把机器从「关机」唤醒；唯一可行的兜底是「开机后 StartWhenAvailable
#     补跑」，而该补跑实测要等 7~10 分钟（10-20 07:28 开机→07:35 触发；10-21 07:24 开机
#     →07:34 触发）。
#   本脚本给任务加一条 BootTrigger（开机后延迟 1 分钟），与既有 04:05 日历触发并存
#   （MultipleInstancesPolicy=IgnoreNew 天然防重），把「开机后出池」从 7~10 分钟压到 1~2 分钟。
#   注意：04:05 日历触发保留——哪天机器夜里没关，仍按原时序在收盘后跑。
#
# 任务原有触发器/动作/其余设置一律 Export→Register 原样保留（含 2026-10-21 已改的动作
# wscript.exe _hidden_runner.vbs）。
# 幂等：重复运行只会有一次 BootTrigger；注册后「回读校验」见 BootTrigger 才算成功。
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

    if ($xml -match '<BootTrigger>') {
        "已有 BootTrigger，跳过注册（幂等）" | Out-File $log -Append -Encoding utf8
    }
    else {
        $xml = $xml -replace '<Triggers>', "<Triggers>`r`n    <BootTrigger><Delay>PT1M</Delay></BootTrigger>"
        Register-ScheduledTask -TaskName $task -Xml $xml -Force | Out-Null
    }

    # 回读校验：必须真的看到 BootTrigger，否则视为未生效（防非终止错误误报成功）
    $t = Get-ScheduledTask -TaskName $task
    $bootTrg = $t.Triggers | Where-Object { $_.CimClass.CimClassName -match 'Boot' }
    $trg = (($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', ')
    if (-not $bootTrg) { throw "回读未见 BootTrigger（实际触发器=$trg）——注册未生效" }

    "OK 触发器=$trg ；BootDelay=$($bootTrg.Delay)" | Out-File $log -Append -Encoding utf8
    "OK WakeToRun=$($t.Settings.WakeToRun) StartWhenAvailable=$($t.Settings.StartWhenAvailable) MultipleInstances=$($t.Settings.MultipleInstances) State=$($t.State)" | Out-File $log -Append -Encoding utf8
    "OK 动作=$($t.Actions[0].Execute) $($t.Actions[0].Arguments)" | Out-File $log -Append -Encoding utf8
}
catch {
    "FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

"==== done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
Write-Host "done: see $log"