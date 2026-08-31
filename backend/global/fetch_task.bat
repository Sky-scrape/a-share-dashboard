@echo off
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
if not exist .status\logs mkdir .status\logs
rem Rotate fetch log when over 1MB: keep last 2 copies
for %%A in (.status\logs\fetch-global.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\fetch-global.log.2 del .status\logs\fetch-global.log.2
  if exist .status\logs\fetch-global.log.1 ren .status\logs\fetch-global.log fetch-global.log.2
  ren .status\logs\fetch-global.log fetch-global.log.1
)
echo [%date% %time%] ==== global fetch start ==== >> .status\logs\fetch-global.log
python backend\global\fetch_global.py >> .status\logs\fetch-global.log 2>&1
echo [%date% %time%] ==== global fetch end ==== >> .status\logs\fetch-global.log
