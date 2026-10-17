@echo off
REM ============================================================
REM  AudioToMP3 (Web) launcher
REM  Batch audio to MP3 converter, served over local HTTP.
REM
REM  Double-click this file to start the server and open a browser.
REM  Close this window (or press Ctrl+C) to stop the server.
REM
REM  All real logic lives in web_server.py; this file only checks
REM  the environment and hands over. Options are passed through:
REM      start_web.bat --port 9000
REM      start_web.bat --host 0.0.0.0 --token [your-secret]
REM      start_web.bat --host :: --port 8765 --token [your-secret]
REM      start_web.bat --host ::1 --port 8765
REM
REM  IPv6 tips: bind "::" for dual stack (IPv6 plus IPv4),
REM  "::1" for IPv6 loopback only, or a full address to bind one
REM  interface. Open the page as http://[IPv6-address]:port/
REM  Always set --token when binding a non-loopback address.
REM
REM  NOTE: keep this file ASCII-only, and never put redirection
REM  symbols (the greater-than or less-than signs) in comments:
REM  cmd.exe parses .bat using the OEM code page, and a stray
REM  redirect symbol would create or spoil a file in this folder.
REM ============================================================

setlocal
cd /d "%~dp0"
title AudioToMP3 Web
chcp 65001 >nul

REM Redirect temp dirs and pip cache into the project folder
set "TMP=%CD%\.tmp"
set "TEMP=%CD%\.tmp"
set "TMPDIR=%CD%\.tmp"
set "PIP_CACHE_DIR=%CD%\.cache\pip"
set "PYTHONUTF8=1"
if not exist ".tmp" mkdir ".tmp" >nul 2>nul
if not exist ".cache\pip" mkdir ".cache\pip" >nul 2>nul

set "VENVPY=%CD%\.venv\Scripts\python.exe"

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

REM 1) No virtual environment yet: create it and install dependencies
if not exist "%VENVPY%" (
    echo [INFO] .venv not found. Preparing environment ^(first run is slow^)...
    %PYEXE% "%~dp0build.py" --env-only
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to prepare the environment. Check your network.
        pause
        exit /b 1
    )
)

REM 2) Venv exists but runtime dependencies are missing: install them now.
REM    This is the fix for "venv present but codecpod not installed".
"%VENVPY%" -c "import codecpod, numpy" 1>nul 2>nul
if errorlevel 1 (
    echo [INFO] Runtime dependencies missing. Installing ^(first run is slow^)...
    "%VENVPY%" "%~dp0build.py" --env-only
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency installation failed. Check your network.
        pause
        exit /b 1
    )
)

REM 3) Start the server
"%VENVPY%" "%~dp0web_server.py" %*
if errorlevel 1 pause
