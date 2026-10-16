# rotation-intraday-fetch 重建 v2（v1 属性/参数名错误且已把旧任务注销，必须立即恢复）
$ErrorActionPreference = 'Stop'
$bat = 'D:\AsusDownload\Desktop\A\backend\rotation\fetch_day_task.bat'
$action = New-ScheduledTaskAction -Execute 'cmd' -Argument ("/c " + $bat)
$trigger = New-ScheduledTaskTrigger -Daily -At '09:25'
$settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'rotation-intraday-fetch' -Action $action -Trigger $trigger -Settings $settings | Out-Null
# 注册后再补日内重复（每 5 分钟重试触发，防常驻进程中途死掉；IgnoreNew 保证不重入）
$t = Get-ScheduledTask -TaskName 'rotation-intraday-fetch'
$t.Triggers[0].Repetition.Interval = 'PT5M'
$t.Triggers[0].Repetition.Duration = 'PT5H45M'
Set-ScheduledTask -InputObject $t | Out-Null
Write-Output 'REGISTERED-OK'
(Get-ScheduledTask -TaskName 'rotation-intraday-fetch') | Select-Object TaskName, State | Format-List
Export-ScheduledTask -TaskName 'rotation-intraday-fetch' | Select-String -Pattern 'Interval|Duration|DisallowStart|StopIfGoing|MultipleInstances|ExecutionTimeLimit' | ForEach-Object { $_.Line.Trim() }
