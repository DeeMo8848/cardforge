@echo off
rem cardforge CLI entry: uses configured venv (settings.json "venv"), fallback to project .venv, callable from any directory
setlocal
set "ROOT=%~dp0"
set "VENV_DIR="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$s = Get-Content '%ROOT%settings.json' -Encoding UTF8 -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($s.venv) { Write-Output $s.venv }"`) do set "VENV_DIR=%%i"
if defined VENV_DIR if not exist "%VENV_DIR%\Scripts\python.exe" set "VENV_DIR="
if not defined VENV_DIR set "VENV_DIR=%ROOT%.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [cardforge] First run detected - running setup.cmd to deploy...
    call "%ROOT%setup.cmd"
    if errorlevel 1 exit /b 1
)
"%VENV_DIR%\Scripts\python.exe" "%ROOT%cardforge.py" %*
exit /b %errorlevel%
