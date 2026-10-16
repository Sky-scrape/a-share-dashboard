# 2026-10-16 复盘缺口事故修复（需管理员权限运行 / UAC）——两件只能提权做的事：
#   1) 给 arecap-daily-fetch 打开 WakeToRun：本机唯一睡眠形态是 S0 低电量待机
#      （网络已断开）。机器在 17:05 前被置入睡眠时触发被错过，StartWhenAvailable
#      只能等下次唤醒补跑（10-15 当晚即 17:33 才补，且补跑中途又断了）。WakeToRun
#      让任务到点自行唤醒机器；配合已开启的 RTCWAKE 唤醒定时器才真正生效。
#   2) 打开任务计划程序 Operational 历史日志：这次最大的证据空洞就是它被禁用——
#      17:05 到底有没有触发、为什么没触发，事后无从查证。
# 只动这两项；触发器/动作/其余设置由 Export→Register 原样保留。
# 幂等：可反复运行。
$ErrorActionPreference = 'Continue'
$log = 'D:\AsusDownload\Desktop\A\.status\logs\fix_recap_wake.log'
$task = 'arecap-daily-fetch'

"==== fix_recap_wake start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8

# --- 1) WakeToRun ---
try {
    $xml = Export-ScheduledTask -TaskName $task
    if ($xml -match '<WakeToRun>') {
        $xml = $xml -replace '<WakeToRun>.*?</WakeToRun>', '<WakeToRun>true</WakeToRun>'
    }
    else {
        $xml = $xml -replace '</Settings>', "  <WakeToRun>true</WakeToRun>`r`n  </Settings>"
    }
    Register-ScheduledTask -TaskName $task -Xml $xml -Force | Out-Null
    $t = Get-ScheduledTask -TaskName $task
    "$task -> WakeToRun=$($t.Settings.WakeToRun) StartWhenAvailable=$($t.Settings.StartWhenAvailable) Limit=$($t.Settings.ExecutionTimeLimit) State=$($t.State)" | Out-File $log -Append -Encoding utf8
}
catch {
    "$task -> FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

# --- 2) 唤醒定时器（非提权也能改，这里只做幂等确认） ---
try {
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    powercfg /S SCHEME_CURRENT | Out-Null
    $q = (powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE) -join "`n"
    $ac = [regex]::Match($q, '交流.*?(0x[0-9a-fA-F]+)').Groups[1].Value
    "RTCWAKE（允许唤醒定时器）当前交流值=$ac （1=启用）" | Out-File $log -Append -Encoding utf8
}
catch {
    "RTCWAKE -> FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

# --- 3) 任务计划程序 Operational 历史日志 ---
try {
    wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
    $e = (wevtutil gl Microsoft-Windows-TaskScheduler/Operational | Select-String 'enabled:')
    "TaskScheduler/Operational -> $e" | Out-File $log -Append -Encoding utf8
}
catch {
    "TaskScheduler/Operational -> FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

"==== done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
Write-Host "完成，明细见 $log"
