@echo off
rem arecap-backup: daily rolling backup of data/ + strategy-iter research db (12:10)
rem ASCII-only + CRLF: cmd misparses BOM/UTF-8 in hidden windows (2026-08-10 lesson)
cd /d "%~dp0.."
rem [data-2 2026-10-06] bare `python` resolved to LibreOffice's bundled interpreter
rem (no numpy/requests); pin the project venv, PATH python as last-resort fallback.
set "PY=.venv-ci\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist .status\logs mkdir .status\logs
echo [%date% %time%] ==== backup start ==== >> .status\logs\backup.log
%PY% backend\backup.py >> .status\logs\backup.log 2>&1
echo [%date% %time%] ==== backup end (rc=%ERRORLEVEL%) ==== >> .status\logs\backup.log
exit /b 0
