@echo off
chcp 65001 >nul
title Sims4Multiplayer 联机启动器
echo ========================================
echo   Sims4Multiplayer 联机启动器 v9.22.1
echo ========================================
echo.

rem ========== 1. 开发机: 源码优先（永远最新——旧打包 exe 会掩盖新功能/修复） ==========
rem     仅当源码存在 且 系统 Python 可用 且 已装 GUI 依赖时走源码，
rem     否则静默落到 exe 回退（避免"双击没反应"）。
if exist "%~dp0launcher.py" (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import customtkinter, PIL" >nul 2>nul
        if not errorlevel 1 (
            cd /d "%~dp0"
            where pythonw >nul 2>nul
            if not errorlevel 1 (
                start "" pythonw launcher.py
            ) else (
                start "" python launcher.py
            )
            echo 正在启动源码版启动器 (v9.22.1)...
            exit /b 0
        )
    )
)

rem ========== 2. 回退: 打包好的 exe（分发包无源码/无依赖时走这里） ==========
set "EXE=%~dp0dist\启动联机\启动联机.exe"
if exist "%EXE%" (
    echo 正在启动打包版启动器...
    start "" "%EXE%"
    exit /b 0
)

rem ========== 3. 回退: 源码目录下的 dist 副本 ==========
set "EXE2=%~dp0启动联机\启动联机.exe"
if exist "%EXE2%" (
    echo 正在启动打包版启动器...
    start "" "%EXE2%"
    exit /b 0
)

rem ========== 4. 回退: 桌面副本 ==========
set "EXE3=%~USERPROFILE%\Desktop\启动联机\启动联机.exe"
if exist "%EXE3%" (
    echo 正在启动桌面版启动器...
    start "" "%EXE3%"
    exit /b 0
)

echo [错误] 未找到可用的启动器（源码依赖缺失且无 exe），请安装 Python + customtkinter 或使用完整分发包
pause
