$evts = Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 400 -ErrorAction SilentlyContinue
$hit = $evts | Where-Object { $_.Message -like '*rotation-intraday*' } | Select-Object -First 14
foreach ($e in $hit) {
  $msg = $e.Message -replace "`r`n", " | "
  if ($msg.Length -gt 220) { $msg = $msg.Substring(0, 220) }
  Write-Output ("{0}  ID={1}  {2}" -f $e.TimeCreated, $e.Id, $msg)
}
