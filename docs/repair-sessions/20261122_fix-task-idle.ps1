# 2026-11-22 计划任务「空闲即杀」就地修复（需管理员 / UAC）
#
# 背景：本机 rotation-intraday-fetch 与 arecap-usclose-fetch 的任务 XML 里仍是
#   <IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd></IdleSettings>
# 盘中用户盯盘时机器不会保持空闲，用户切走/回来会触发空闲状态变化，任务被判
# 「空闲结束」杀掉（复盘事故-H-01/H-04）。本脚本只做一件事：
#   Export-ScheduledTask -> 仅把 StopOnIdleEnd 改为 false（缺失 IdleSettings 则补）
#   -> Register-ScheduledTask -Xml -Force -> 回读校验 StopOnIdleEnd=false。
#
# 边界：
#   - 只改上述两个任务；触发器/动作/其余设置由 Export -> Register 原样保留。
#   - 首次运行把改动前 XML 备份到 .status/task-backup/<task>-idle.xml（已存在不覆盖）。
#   - 幂等：已是 false 的任务跳过；可反复运行。
#   - 不启动/不触发任何任务 —— 本脚本不会自动运行采集，改完由人工执行并回读。
#   - 需管理员：改计划任务在本机是 admin-only（2026-10-16/10-21 同因）。
$ErrorActionPreference = 'Stop'
$root = 'D:\AsusDownload\Desktop\A'
$backupDir = Join-Path $root '.status\task-backup'
$log = Join-Path $root '.status\logs\fix_task_idle.log'
$tasks = @('rotation-intraday-fetch', 'arecap-usclose-fetch')

function Say([string]$msg) {
    Write-Output $msg
    Add-Content -Path $log -Value $msg -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
Say ("==== fix_task_idle start " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " ====")

try {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Not elevated: 请右键「以管理员运行」本脚本（UAC -> 是）。'
    }

    foreach ($task in $tasks) {
        try {
            $current = (Get-ScheduledTask -TaskName $task).Settings.StopOnIdleEnd
            if ($current -eq $false) {
                Say ("[skip] " + $task + " StopOnIdleEnd 已是 false")
                continue
            }

            $xml = [string](Export-ScheduledTask -TaskName $task)
            $bak = Join-Path $backupDir ($task + '-idle.xml')
            if (-not (Test-Path $bak)) {
                # 用 UTF-16 与 Export-ScheduledTask 的 encoding="UTF-16" 声明对齐
                Set-Content -Path $bak -Value $xml -Encoding Unicode
                Say ("[backup] " + $bak)
            }
            else {
                Say ("[backup] 已存在，不覆盖：" + $bak)
            }

            if ($xml -match '<IdleSettings\s*/>') {
                $xml = $xml -replace '<IdleSettings\s*/>', '<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>'
            }
            elseif ($xml -match '<IdleSettings>') {
                if ($xml -match '<StopOnIdleEnd>') {
                    $xml = $xml -replace '<StopOnIdleEnd>[^<]*</StopOnIdleEnd>', '<StopOnIdleEnd>false</StopOnIdleEnd>'
                }
                else {
                    $xml = $xml -replace '</IdleSettings>', '<StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>'
                }
            }
            else {
                $xml = $xml -replace '</Settings>', "  <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>`r`n</Settings>"
            }

            Register-ScheduledTask -TaskName $task -Xml $xml -Force | Out-Null
            $after = (Get-ScheduledTask -TaskName $task).Settings.StopOnIdleEnd
            $afterXml = [string](Export-ScheduledTask -TaskName $task)
            if ($after -ne $false -or $afterXml -notmatch '<StopOnIdleEnd>false</StopOnIdleEnd>') {
                throw ("回读校验失败：StopOnIdleEnd=" + $after)
            }
            Say ("[ok] " + $task + " StopOnIdleEnd=false（回读通过）")
        }
        catch {
            Say ("[fail] " + $task + " -> " + $_.Exception.Message)
            throw
        }
    }
    Say '请手动执行并回读：本脚本不自动运行任何任务；执行后用 Get-ScheduledTask 回读 StopOnIdleEnd。'
}
catch {
    Say ("[abort] " + $_.Exception.Message)
    Say '请手动执行并回读：修复未完成，请按上方错误处理后重跑。'
    exit 1
}
