@echo off
rem arecap-autostart: start dashboard service at logon (minimized, no browser)
cd /d "%~dp0.."
rem [data-2 2026-10-06] bare `python` resolved to LibreOffice's bundled interpreter
rem (no numpy/requests); pin the project venv, PATH python as last-resort fallback.
set "PY=.venv-ci\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
start "arecap-supervisor" /min "%PY%" start.py --no-browser
exit /b 0
