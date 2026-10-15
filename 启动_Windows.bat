@echo off
chcp 65001 >nul
rem 网舆监控中台 —— Windows 一键启动。双击本文件即可。
cd /d "%~dp0"

echo ==============================================
echo   网舆 . 舆论声量监控中台
echo ==============================================

rem 找 Python：优先 py 启动器，其次 python
set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" (
  where python >nul 2>&1 && set PY=python
)
if "%PY%"=="" (
  echo [X] 没找到 Python。请到 https://www.python.org/downloads/ 安装
  echo     安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 (
  echo [X] Python 版本低于 3.10，请升级后重试。
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo [OK] %%v

rem 虚拟环境：依赖装在项目里，不污染系统 Python
if not exist ".venv" (
  echo -^> 首次运行，创建虚拟环境...
  %PY% -m venv .venv || (echo [X] 创建虚拟环境失败 & pause & exit /b 1)
)
call .venv\Scripts\activate.bat

if not exist ".venv\.deps_ok" (
  echo -^> 安装 Python 依赖（约 1 分钟）...
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt || (echo [X] 依赖安装失败，检查网络 & pause & exit /b 1)
  echo ok> .venv\.deps_ok
)

if not exist ".venv\.browser_ok" (
  echo -^> 下载浏览器内核 Chromium（约 150MB，只需一次）...
  if "%PLAYWRIGHT_DOWNLOAD_HOST%"=="" set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
  python -m playwright install chromium || (
    echo [X] 浏览器下载失败。可手动执行: python -m playwright install chromium
    pause & exit /b 1)
  echo ok> .venv\.browser_ok
)

echo.
echo -^> 启动中台，浏览器会自动打开 http://127.0.0.1:8765
echo    关闭这个窗口就是停止服务。
echo.
python -m hub.cli serve
pause
