@echo off
rem arecap-watchdog: probe local dashboard every 15 min, alert when down
cd /d "%~dp0.."
rem [data-2 2026-10-06] bare `python` resolved to LibreOffice's bundled interpreter
rem (no numpy/requests); alert chain (`import notify`) crashed silently for days.
rem Pin the project venv; PATH python only as last-resort fallback.
set "PY=.venv-ci\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist .status\logs mkdir .status\logs
for %%A in (.status\logs\watchdog.log) do if %%~zA GEQ 1048576 (
  if exist .status\logs\watchdog.log.2 del .status\logs\watchdog.log.2
  if exist .status\logs\watchdog.log.1 ren .status\logs\watchdog.log watchdog.log.2
  ren .status\logs\watchdog.log watchdog.log.1
)
%PY% backend\watchdog.py >> .status\logs\watchdog.log 2>&1
exit /b 0
