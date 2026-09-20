@echo off
rem cardforge one-click deploy: create venv + install dependencies
rem venv 位置来源优先级：settings.json "venv" 配置项 > 项目自带 .venv
rem Models (~2GB) are not in the repo, auto-downloaded by rembg to models/
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "VENV_DIR="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$s = Get-Content '%ROOT%settings.json' -Encoding UTF8 -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($s.venv) { Write-Output $s.venv }"`) do set "VENV_DIR=%%i"
if defined VENV_DIR if not exist "%VENV_DIR%\Scripts\python.exe" set "VENV_DIR="
if not defined VENV_DIR set "VENV_DIR=%ROOT%.venv"

if exist "%VENV_DIR%\Scripts\python.exe" (
    echo [cardforge] venv already exists at %VENV_DIR%, skip creation
) else (
    echo [cardforge] Creating virtual environment %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [cardforge] Failed: please install Python 3.10+ and add it to PATH
        pause
        exit /b 1
    )
)

echo [cardforge] Installing dependencies ...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo [cardforge] Dependency install failed, check your network and retry
    pause
    exit /b 1
)

echo.
echo [cardforge] Deploy done! Start the web UI with webui.cmd
pause
