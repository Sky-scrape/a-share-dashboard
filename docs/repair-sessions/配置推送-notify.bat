@echo off
setlocal
cd /d "%~dp0..\.."
echo Fill ONE channel in .status\notify.json, save and close Notepad.
start /wait "" notepad ".status\notify.json"
".venv-ci\Scripts\python.exe" backend\notify.py --send
echo.
pause
endlocal
