@echo off
rem arecap-autostart: start dashboard service at logon (minimized, no browser)
cd /d "%~dp0.."
start "arecap-supervisor" /min python start.py --no-browser
exit /b 0
