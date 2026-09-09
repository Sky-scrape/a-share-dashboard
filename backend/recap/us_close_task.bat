@echo off
rem 美股收盘后复盘完成链（C6 时序改造）：等待美股收盘+20min（脚本内 DST 感知等待）
rem 后，抓取隔夜美股 -> 完成 T 日备选池/验证 -> 校验 -> 派生面板。
rem 计划任务 arecap-usclose-fetch 每日 04:05 触发；17:05 的 fetch_task.bat 只负责
rem A 股数据落盘，两者时序契约见 backend/recap/us_close_task.py 模块注释。
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
if not exist .status\logs mkdir .status\logs
rem Rotate usclose log when over 1MB: keep last 2 copies
for %%A in (.status\logs\usclose.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\usclose.log.2 del .status\logs\usclose.log.2
  if exist .status\logs\usclose.log.1 ren .status\logs\usclose.log usclose.log.2
  ren .status\logs\usclose.log usclose.log.1
)
echo [%date% %time%] ==== bat invoked (task fired) ==== >> .status\logs\usclose.log
python backend\recap\us_close_task.py >> .status\logs\usclose.log 2>&1
exit /b %ERRORLEVEL%
