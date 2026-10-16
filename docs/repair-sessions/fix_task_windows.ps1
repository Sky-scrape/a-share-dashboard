# 修复抓取任务弹终端窗：把 Interactive 登录类型的看板任务转为 S4U（不弹窗、不存密码）。
# 需管理员权限运行（UAC）。只改 LogonType，触发器/动作/其余设置原样保留。
$ErrorActionPreference = 'Continue'
$log   = 'D:\AsusDownload\Desktop\A\.status\logs\fix_task_windows.log'
$tasks = @('aglobal-daily-fetch', 'AKBoard8000', 'areauction-live-fetch', 'rotation-intraday-fetch')

"==== fix_task_windows start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
foreach ($n in $tasks) {
    try {
        $xml = Export-ScheduledTask -TaskName $n
        if ($xml -match '<LogonType>InteractiveToken</LogonType>') {
            $xml = $xml -replace '<LogonType>InteractiveToken</LogonType>', '<LogonType>S4U</LogonType>'
            Register-ScheduledTask -TaskName $n -Xml $xml -Force | Out-Null
            $t = Get-ScheduledTask -TaskName $n
            "$n -> OK, LogonType=$($t.Principal.LogonType), State=$($t.State)" | Out-File $log -Append -Encoding utf8
        }
        else {
            "$n -> SKIP (LogonType is not InteractiveToken)" | Out-File $log -Append -Encoding utf8
        }
    }
    catch {
        "$n -> FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
}
"==== done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File $log -Append -Encoding utf8
