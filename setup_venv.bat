@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem ============================================
rem   病理裁剪工具 - 环境配置
rem   创建 .venv312 并安装核心依赖
rem ============================================

echo ============================================
echo   病理裁剪工具 - 环境配置
echo ============================================
echo.

set "VENV_DIR=%CD%\.venv312"

rem ---------- 1. 定位 Python 解释器 ----------
set "PYEXE="

where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=py -3.12"
        goto :gotpy
    )
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=py -3"
        goto :gotpy
    )
)

where python >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python"
    goto :gotpy
)

goto :nopy

:gotpy
echo [1/4] 解释器: %PYEXE%
%PYEXE% -c "import sys; print('       版本: Python ' + '.'.join(map(str, sys.version_info[:3])))"
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo.
    echo [错误] 需要 Python 3.10 或更高版本。
    echo.
    pause
    exit /b 1
)

rem ---------- 2. 创建虚拟环境 ----------
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo [2/4] 虚拟环境已存在，跳过创建
) else (
    echo [2/4] 创建虚拟环境: %VENV_DIR%
    %PYEXE% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo [错误] 虚拟环境创建失败。
        pause
        exit /b 1
    )
)

set "VPY=%VENV_DIR%\Scripts\python.exe"

rem ---------- 3. 升级 pip ----------
echo [3/4] 升级 pip ...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 echo       [警告] pip 升级失败，继续安装依赖

rem ---------- 4. 安装依赖 ----------
echo [4/4] 安装核心依赖（约 1-2 分钟）...
"%VPY%" -m pip install -e .
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。
    echo       也可改用: "%VPY%" -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
echo ============================================
echo   配置完成
echo ============================================
echo.
echo 可选 — 安装 AI 功能依赖（DeepLIIF，约 5GB）:
echo        "%VPY%" -m pip install -e ".[ai]"
echo.
echo 启动程序: run.bat
echo.
pause
exit /b 0

:nopy
echo.
echo [错误] 未找到 Python 解释器。
echo.
echo 请先安装 Python 3.10+ （安装时勾选 Add Python to PATH）:
echo        https://www.python.org/downloads/
echo.
pause
exit /b 1
