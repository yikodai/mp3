@echo off
REM ============================================================
REM  AudioToMP3 - build launcher
REM  音频转 MP3 便携工具 - 构建入口
REM
REM  这里只做环境检查，真正的构建逻辑全部在 build.py 中
REM  （用 Python 输出中文更可靠，避免批处理的编码问题）。
REM
REM  用法：双击本文件，或在命令行执行 build.bat
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
    echo         Please install Python 3.10 - 3.13 ^(64-bit^) first:
    echo         https://www.python.org/downloads/windows/
    echo.
    pause
    exit /b 1
)

%PYEXE% "%~dp0build.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See the messages above.
    echo.
    pause
    exit /b 1
)

echo.
echo Done. See dist\AudioToMP3.exe
pause
