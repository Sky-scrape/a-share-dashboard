@echo off
setlocal
cd /d "%~dp0..\.."
".venv-ci\Scripts\python.exe" tools\check.py
pause
endlocal
