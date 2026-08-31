@echo off
chcp 65001 >nul
title A股看板
cd /d "."
echo ============================================
echo  A股看板启动中...
echo  看板地址  http://127.0.0.1:8000/
echo  浏览器将自动打开（首次约需 3-5 秒）
echo ============================================
python start.py
pause
