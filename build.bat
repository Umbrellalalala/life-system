@echo off
chcp 65001 >nul
echo ============================================
echo   Life System - 打包 EXE（单文件模式）
echo ============================================

rem 使用系统 Python（有 PyInstaller + PySide6）
set "PYTHON=python"

%PYTHON% -m PyInstaller --noconfirm --onefile --windowed ^
    --name LifeSystem ^
    --icon assets\icon.ico ^
    --add-data "assets;assets" ^
    --collect-submodules markdown ^
    --hidden-import PySide6.QtMultimedia ^
    run.py

echo.
echo 打包完成：dist\LifeSystem.exe
pause
