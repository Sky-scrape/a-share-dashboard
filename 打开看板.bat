@echo off
chcp 65001 >nul
title A股看板
cd /d "%~dp0"
echo ============================================
echo  A股看板启动中...
echo  看板地址  http://127.0.0.1:8000/
echo  交易日 09:10-09:30 打开时自动先落实时竞价页（/auction）
echo  浏览器将自动打开（首次约需 3-5 秒）
echo ============================================
python start.py
pause
