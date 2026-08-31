@echo off
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
if not exist .status\logs mkdir .status\logs
rem Rotate fetch log when over 1MB: keep last 2 copies
for %%A in (.status\logs\fetch-rotation.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\fetch-rotation.log.2 del .status\logs\fetch-rotation.log.2
  if exist .status\logs\fetch-rotation.log.1 ren .status\logs\fetch-rotation.log fetch-rotation.log.2
  ren .status\logs\fetch-rotation.log fetch-rotation.log.1
)
echo [%date% %time%] ==== rotation fetch start ==== >> .status\logs\fetch-rotation.log
rem 同花顺口径采集器：常驻循环至收盘自退出（盘外启动则定格一轮后退出）
python backend\rotation\ths_collect.py >> .status\logs\fetch-rotation.log 2>&1
set RC_FETCH=%ERRORLEVEL%
rem refresh derived rotation panels (stats / strength matrix)
python backend\derive.py >> .status\logs\fetch-rotation.log 2>&1
echo [%date% %time%] ==== rotation fetch end (fetch=%RC_FETCH%) ==== >> .status\logs\fetch-rotation.log
exit /b %RC_FETCH%
