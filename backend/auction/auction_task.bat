@echo off
rem 实时竞价定时任务入口（建议交易日 09:14 触发；采集器内部自行等待 09:15-09:25 时间线）
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
if not exist .status\logs mkdir .status\logs
rem Rotate log when over 1MB: keep last 2 copies
for %%A in (.status\logs\fetch-auction.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\fetch-auction.log.2 del .status\logs\fetch-auction.log.2
  if exist .status\logs\fetch-auction.log.1 ren .status\logs\fetch-auction.log fetch-auction.log.2
  ren .status\logs\fetch-auction.log fetch-auction.log.1
)
echo [%date% %time%] ==== auction start ==== >> .status\logs\fetch-auction.log
python backend\auction\auc_collector.py >> .status\logs\fetch-auction.log 2>&1
echo [%date% %time%] ==== auction end ==== >> .status\logs\fetch-auction.log
