@echo off
rem arecap-watchdog: probe local dashboard every 15 min, alert when down
cd /d "%~dp0.."
if not exist .status\logs mkdir .status\logs
for %%A in (.status\logs\watchdog.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\watchdog.log.2 del .status\logs\watchdog.log.2
  if exist .status\logs\watchdog.log.1 ren .status\logs\watchdog.log watchdog.log.2
  ren .status\logs\watchdog.log watchdog.log.1
)
python backend\watchdog.py --once >> .status\logs\watchdog.log 2>&1
exit /b 0
