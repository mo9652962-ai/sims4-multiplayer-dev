@echo off
chcp 65001 >nul
title Sims4Multiplayer 联机启动器
echo ========================================
echo   Sims4Multiplayer 联机启动器 v9.20.5
echo ========================================
echo.

rem ========== 1. 优先启动打包好的 exe（不依赖 Python） ==========
set "EXE=%~dp0dist\启动联机\启动联机.exe"
if exist "%EXE%" (
    echo 正在启动打包版启动器...
    start "" "%EXE%"
    exit /b 0
)

rem ========== 2. 回退: 源码目录下的 dist 副本 ==========
set "EXE2=%~dp0启动联机\启动联机.exe"
if exist "%EXE2%" (
    echo 正在启动打包版启动器...
    start "" "%EXE2%"
    exit /b 0
)

rem ========== 3. 回退: 桌面副本 ==========
set "EXE3=%USERPROFILE%\Desktop\启动联机\启动联机.exe"
if exist "%EXE3%" (
    echo 正在启动桌面版启动器...
    start "" "%EXE3%"
    exit /b 0
)

rem ========== 4. 最后回退: 系统 Python 直接跑源码 ==========
echo [提示] 未找到打包版 exe，尝试用 Python 启动源码...
cd /d D:\Sims4-Multiplayer-Dev
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw launcher.py
    exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python launcher.py
    exit /b 0
)

echo [错误] 未找到启动器 exe 或 Python，请确认已解压完整包
pause
