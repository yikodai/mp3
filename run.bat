@echo off
REM ============================================================
REM  AudioToMP3 - run from source
REM  音频转 MP3 便携工具 - 从源码启动（自动使用项目自带的虚拟环境）
REM
REM  重要：请用本脚本启动源码版本，不要直接双击 main.py，
REM        否则会用系统 Python 运行，从而报
REM        “无法加载 codecpod 音频引擎: No module named 'codecpod'”。
REM
REM  如果只想运行打包好的单文件程序，请直接双击
REM  dist\AudioToMP3.exe。
REM ============================================================

setlocal
cd /d "%~dp0"

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python not found in PATH.
    echo         Please install Python 3.10 - 3.13 ^(64-bit^) first.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found, preparing it now ^(first run^)...
    %PYEXE% "%~dp0build.py" --env-only
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to prepare the environment.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" "%~dp0main.py"
if errorlevel 1 pause
