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
echo.
echo 打包完成: dist\ak-dashboard\ak-dashboard.exe
echo 整个 ak-dashboard 目录才是完整程序；data 与 .status 生成在 _internal 下
pause
exit /b 0
:err
echo.
echo 打包失败，请检查上方报错。
pause
exit /b 1
