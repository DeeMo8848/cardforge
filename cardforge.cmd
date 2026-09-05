@echo off
rem cardforge 便捷入口：自动使用项目内虚拟环境，可在任意目录调用
rem 首次使用自动初始化虚拟环境（setup.cmd），之后直接调用
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [cardforge] 检测到未初始化，首次使用将自动部署（创建 .venv 并安装依赖）...
    call "%ROOT%setup.cmd"
    if errorlevel 1 exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%cardforge.py" %*
exit /b %errorlevel%
