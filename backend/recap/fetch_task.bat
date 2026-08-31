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
python backend\recap\fetch_daily.py >> .status\logs\fetch-recap.log 2>&1
set RC_FETCH=%ERRORLEVEL%
rem hithink local DuckDB incremental sync (research db, daily) - auxiliary; failure must NOT fail the recap chain
rem Default DuckDB memory cap is 1GiB which breaks sync commit on this box; give it 4GiB (machine has 32GB)
set HITHINK_FINANCE_DUCKDB_MEMORY_LIMIT=4GiB
call hithink-finance data sync --format json >> .status\logs\fetch-recap.log 2>&1
if errorlevel 1 echo [warn] hithink data sync failed (non-blocking) >> .status\logs\fetch-recap.log
for /f %%i in ('powershell -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
rem speculation deviation radar needs today's daily bars which only exist AFTER the sync above;
rem fetch_daily ran before sync, so recompute the speculation module here (non-blocking)
python backend\recap\speculate.py --date %TODAY% >> .status\logs\fetch-recap.log 2>&1
if errorlevel 1 echo [warn] speculation retrofill failed (non-blocking) >> .status\logs\fetch-recap.log
rem concept membership map refresh: no-op unless older than 7 days (rebuild takes minutes, weekly)
python backend\recap\concept_map.py >> .status\logs\fetch-recap.log 2>&1
if errorlevel 1 echo [warn] concept_map refresh failed (non-blocking) >> .status\logs\fetch-recap.log
python backend\recap\check_snapshot.py data\recap\%TODAY%.json >> .status\logs\fetch-recap.log 2>&1
set RC_CHECK=%ERRORLEVEL%
rem derived panels (sentiment / rotation stats / strength matrix)
python backend\derive.py >> .status\logs\fetch-recap.log 2>&1
rem gzip archive snapshots older than 14 days
python backend\recap\archive.py --older-than 14 >> .status\logs\fetch-recap.log 2>&1
echo [%date% %time%] ==== fetch end (fetch=%RC_FETCH% check=%RC_CHECK%) ==== >> .status\logs\fetch-recap.log
if not "%RC_FETCH%"=="0" exit /b %RC_FETCH%
if not "%RC_CHECK%"=="0" exit /b %RC_CHECK%
exit /b 0
