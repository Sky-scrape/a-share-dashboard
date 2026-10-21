@echo off
rem One-click launcher for 20261021_usclose_boot_trigger.ps1
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
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp020261021_usclose_boot_trigger.ps1"

echo.
echo ==== repair log (tail) ====
powershell -NoProfile -Command "Get-Content -Encoding UTF8 '%~dp0..\..\.status\logs\fix_usclose_boot.log' -Tail 12"

echo.
echo Success looks like:
echo   OK ???=MSFT_TaskBootTrigger, MSFT_TaskDailyTrigger ...
echo   OK ??=C:/Windows/System32/wscript.exe ...uc_close_task.bat...
echo.
pause