@echo off
chcp 65001 >nul
title AK 看板 · 打包 exe
cd /d "%~dp0"
echo ============================================
echo  AK 看板 - 打包 Windows exe（PyInstaller onedir）
echo ============================================
if exist .venv-ci\Scripts\python.exe (
    set "PY=.venv-ci\Scripts\python.exe"
) else (
    set "PY=python"
)
%PY% -m pip install -q pyinstaller==6.17.0 || goto :err
%PY% -m PyInstaller packaging\ak-dashboard.spec --noconfirm --distpath dist --workpath build || goto :err
set AK_ONEFILE=1
%PY% -m PyInstaller packaging\ak-dashboard.spec --noconfirm --distpath dist --workpath build || goto :err
rem 版本单一来源 backend/version.py：单文件产物名自动带版本号（2026-09-27 起，此前 v1.2.0 为手工命名）
rem ak_ver.txt 加 PID 后缀防多会话并发互踩（P2-5，2026-10-19）
set "AK_VER_TXT=%TEMP%\ak_ver_%RANDOM%%RANDOM%.txt"
%PY% -c "import sys; sys.path.insert(0,'backend'); import version; print(version.version_string(), end='')" > "%AK_VER_TXT%" 2>nul
set /p VER=<"%AK_VER_TXT%"
del "%AK_VER_TXT%" >nul 2>&1
if "%VER%"=="" set VER=unknown
if exist dist\ak-dashboard-onefile.exe ren dist\ak-dashboard-onefile.exe ak-dashboard-onefile-%VER%.exe
echo.
echo 打包完成（%VER%）:
echo   解压版   dist\ak-dashboard\ak-dashboard.exe（整个目录才是完整程序）
echo   单文件版 dist\ak-dashboard-onefile-%VER%.exe（双击即用；数据在 %%LOCALAPPDATA%%\ak-dashboard）
echo   两种形态双击默认开独立桌面窗口，全程无终端黑窗（不弹浏览器，关窗即退出）；
echo   --web 切回浏览器模式，--console 现场开控制台看日志。
pause
exit /b 0
:err
echo.
echo 打包失败，请检查上方报错。
pause
exit /b 1
