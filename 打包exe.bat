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
%PY% -m pip install -q pyinstaller || goto :err
%PY% -m PyInstaller packaging\ak-dashboard.spec --noconfirm --distpath dist --workpath build || goto :err
set AK_ONEFILE=1
%PY% -m PyInstaller packaging\ak-dashboard.spec --noconfirm --distpath dist --workpath build || goto :err
echo.
echo 打包完成:
echo   解压版   dist\ak-dashboard\ak-dashboard.exe（整个目录才是完整程序）
echo   单文件版 dist\ak-dashboard-onefile.exe（双击即用；数据在 %%LOCALAPPDATA%%\ak-dashboard）
pause
exit /b 0
:err
echo.
echo 打包失败，请检查上方报错。
pause
exit /b 1
