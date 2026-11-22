@echo off
setlocal
cd /d "%~dp0..\.."
echo ==== %DATE% %TIME% ==== >> ".status\logs\probe-global.log"
".venv-ci\Scripts\python.exe" tools\probe_global_cn.py --limit 50 --verbose >> ".status\logs\probe-global.log" 2>&1
echo Done. Log: .status\logs\probe-global.log
pause
endlocal
