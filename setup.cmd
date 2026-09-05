@echo off
rem ============================================================
rem  cardforge 一键部署：创建虚拟环境 + 安装依赖
rem  首次克隆后运行一次即可；之后直接使用 webui.cmd / cardforge.cmd
rem  模型约 2GB 不在仓库内，首次抠图时由 rembg 自动下载到 models/
rem ============================================================
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if exist "%ROOT%.venv\Scripts\python.exe" (
    echo [cardforge] 已存在 .venv，跳过创建
) else (
    echo [cardforge] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [cardforge] 创建失败：请确认已安装 Python 3.10+ 且已加入 PATH
        pause
        exit /b 1
    )
)

echo [cardforge] 安装依赖 ...
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [cardforge] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)

echo.
echo [cardforge] 部署完成！使用 webui.cmd 启动 Web 界面
pause
