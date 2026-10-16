@echo off
rem One-click launcher for 20261016_fix_recap_wake_and_tasklog.ps1
rem Needs Administrator: changing a scheduled task is admin-only on this box.
rem Right-click this file -> "Run as administrator" (UAC -> Yes).
rem ASCII-only on purpose: no chcp/encoding pitfalls in cmd.
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Not running as administrator.
  echo.
  echo Close this window, then RIGHT-CLICK this .bat
  echo and choose "Run as administrator" ^(UAC -^> Yes^).
  echo.
  pause
  exit /b 1
)

echo [ok] elevated. running repair script...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp020261016_fix_recap_wake_and_tasklog.ps1"

echo.
echo ==== repair log (tail) ====
powershell -NoProfile -Command "Get-Content -Encoding UTF8 '%~dp0..\..\.status\logs\fix_recap_wake.log' -Tail 12"

echo.
echo Success looks like:
echo   arecap-daily-fetch -^> WakeToRun=True ...
echo   TaskScheduler/Operational -^> enabled: true
echo.
pause
