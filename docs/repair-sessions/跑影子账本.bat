@echo off
setlocal
cd /d "%~dp0..\.."
".venv-ci\Scripts\python.exe" strategy-iter\scripts\shadow_0_2_ledger.py --since 20261122
echo.
echo Opening ledger.csv ... fill fill/fill_price/size/fee/exit/net.
start "" "strategy-iter\runs\shadow_0_2\ledger.csv"
pause
endlocal
