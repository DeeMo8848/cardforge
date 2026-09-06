@echo off
rem cardforge CLI entry: uses project venv, callable from any directory
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [cardforge] First run detected - running setup.cmd to deploy...
    call "%ROOT%setup.cmd"
    if errorlevel 1 exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%cardforge.py" %*
exit /b %errorlevel%
