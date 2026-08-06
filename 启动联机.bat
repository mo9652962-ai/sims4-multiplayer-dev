@echo off
chcp 65001 >nul
title Sims4Multiplayer 联机启动器
echo ========================================
echo   Sims4Multiplayer 联机启动器 v5.3
echo ========================================
echo.
cd /d D:\Sims4-Multiplayer-Dev

rem 优先用系统 python 启动 GUI（pythonw 无黑窗口）
where pythonw >nul 2>nul
if %errorlevel%==0 (
    echo 正在启动 GUI...
    start "" pythonw launcher.py
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    echo 正在启动 GUI (pythonw 不可用，用 python 代替)...
    start "" python launcher.py
    exit /b 0
)

echo [错误] 未找到 Python，请先安装 Python 3.x
pause
