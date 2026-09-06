@echo off
rem cardforge web launcher: auto-bootstraps on first run
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [cardforge] First run detected - running setup.cmd to deploy...
    call "%ROOT%setup.cmd"
    if errorlevel 1 exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%webapp.py" %*
if errorlevel 1 (
    echo.
    echo [cardforge] Failed to start webapp.py, see error message above.
    pause
)
exit /b %errorlevel%
