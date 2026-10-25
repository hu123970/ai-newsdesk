@echo off
setlocal
cd /d "%~dp0"

rem Make the console print Chinese correctly (this file is ASCII-only on purpose:
rem a UTF-8 batch file with non-ASCII text breaks cmd parsing).
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ============================================
echo   AI NewsDesk - AI News Aggregator
echo ============================================
echo.

rem ---- 1. Find a working Python 3 ----
rem NOTE: "python.exe" on Windows may be the Microsoft Store stub, which does
rem       nothing (exit code 9009). "py -3" is the reliable launcher.
set "PY="
py -3 -V >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    py -V >nul 2>nul
    if not errorlevel 1 set "PY=py"
)
if not defined PY (
    python3 -V >nul 2>nul
    if not errorlevel 1 set "PY=python3"
)
if not defined PY (
    python -V >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] No working Python 3 found.
    echo         Install Python 3.11+ and tick "Add python.exe to PATH".
    echo         Then run this file again.
    goto :end
)

echo Python  : %PY%
%PY% -V
echo.

rem ---- 2. Check dependencies, install if missing ----
%PY% -c "import fastapi, uvicorn, jinja2, feedparser, httpx, bs4, openai, apscheduler, dotenv, Crypto" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing requirements.txt ...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check your network and retry.
        goto :end
    )
    echo.
)

rem ---- 3. Warn if port 9999 is already in use ----
netstat -ano | findstr /R /C:":9999 .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [WARN] Port 9999 is already in use.
    echo        Maybe this app is already running in another window.
    echo        Otherwise change PORT in .env
    echo.
)

echo Starting ... open http://127.0.0.1:9999 in your browser.
echo Close this window to stop the server.
echo.
%PY% main.py

echo.
echo [EXIT] The server has stopped. Screenshot the output above if it failed.
:end
echo.
pause
