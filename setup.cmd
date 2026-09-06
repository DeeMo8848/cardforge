@echo off
rem cardforge one-click deploy: create .venv + install dependencies
rem Run once after cloning; then use webui.cmd / cardforge.cmd
rem Models (~2GB) are not in the repo, auto-downloaded by rembg to models/
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if exist "%ROOT%.venv\Scripts\python.exe" (
    echo [cardforge] .venv already exists, skip creation
) else (
    echo [cardforge] Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [cardforge] Failed: please install Python 3.10+ and add it to PATH
        pause
        exit /b 1
    )
)

echo [cardforge] Installing dependencies ...
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [cardforge] Dependency install failed, check your network and retry
    pause
    exit /b 1
)

echo.
echo [cardforge] Deploy done! Start the web UI with webui.cmd
pause
