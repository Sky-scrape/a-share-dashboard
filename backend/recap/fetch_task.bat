@echo off
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
if not exist .status\logs mkdir .status\logs
rem Rotate fetch log when over 1MB: keep last 2 copies
for %%A in (.status\logs\fetch-recap.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\fetch-recap.log.2 del .status\logs\fetch-recap.log.2
  if exist .status\logs\fetch-recap.log.1 ren .status\logs\fetch-recap.log fetch-recap.log.2
  ren .status\logs\fetch-recap.log fetch-recap.log.1
)
echo [%date% %time%] ==== fetch start ==== >> .status\logs\fetch-recap.log
rem C6 时序改造（2026-09-09）：17:05 只做 A 股数据落盘；备选池/快照校验/派生面板
rem 移到次日美股收盘后 1 小时内的 us_close_task.bat（arecap-usclose-fetch，04:05
rem 触发），使 T 日备选池生成时已包含隔夜美股场次与 C6 环境闸门。
python backend\recap\fetch_daily.py >> .status\logs\fetch-recap.log 2>&1
set RC_FETCH=%ERRORLEVEL%
rem hithink local DuckDB incremental sync (research db, daily) - auxiliary; failure must NOT fail the recap chain
rem Default DuckDB memory cap is 1GiB which breaks sync commit on this box; give it 4GiB (machine has 32GB)
set HITHINK_FINANCE_DUCKDB_MEMORY_LIMIT=4GiB
call hithink-finance data sync --format json >> .status\logs\fetch-recap.log 2>&1
if errorlevel 1 echo [warn] hithink data sync failed (non-blocking) >> .status\logs\fetch-recap.log
rem concept membership map refresh: no-op unless older than 7 days (rebuild takes minutes, weekly)
python backend\recap\concept_map.py >> .status\logs\fetch-recap.log 2>&1
if errorlevel 1 echo [warn] concept_map refresh failed (non-blocking) >> .status\logs\fetch-recap.log
rem gzip archive snapshots older than 14 days
python backend\recap\archive.py --older-than 14 >> .status\logs\fetch-recap.log 2>&1
rem 全球总览收盘后刷新：CN 热力图数据源是**实时快照**，08:40 盘前抓不到当日成交数据，
rem 只靠盘前那一轮会让 A 股热力图长期停在旧副本（2026-09-10 实测停在 3 天前）；
rem 非阻塞，失败只记 [warn]（盘前/非交易日快照为空属预期，脚本内已按预期处理）
python backend\global\fetch_global.py >> .status\logs\fetch-global.log 2>&1
if errorlevel 1 echo [warn] global afterclose refresh failed (non-blocking) >> .status\logs\fetch-recap.log
echo [%date% %time%] ==== fetch end (fetch=%RC_FETCH%) ==== >> .status\logs\fetch-recap.log
if not "%RC_FETCH%"=="0" exit /b %RC_FETCH%
exit /b 0
