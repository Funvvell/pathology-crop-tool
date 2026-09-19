@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem ============================================
rem   病理裁剪工具 - 启动脚本
rem   自动查找可用的 Python 解释器
rem ============================================

set "PYEXE="

rem 1) 优先使用项目虚拟环境（.venv312 / venv / .venv）
if exist ".venv312\Scripts\python.exe" set "PYEXE=%CD%\.venv312\Scripts\python.exe"
if not defined PYEXE if exist "venv\Scripts\python.exe" set "PYEXE=%CD%\venv\Scripts\python.exe"
if not defined PYEXE if exist ".venv\Scripts\python.exe" set "PYEXE=%CD%\.venv\Scripts\python.exe"

rem 2) 回退到系统 PATH 中的 python
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto :nopy

rem 检查核心依赖是否可用
"%PYEXE%" -c "import PySide6, sdpc, tifffile, skimage, scipy" >nul 2>&1
if errorlevel 1 goto :nodeps

echo 正在启动病理裁剪工具...
"%PYEXE%" main.py
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出。
    echo       日志位置: %USERPROFILE%\pathology-crop-tool.log
    echo.
    pause
)
exit /b 0

:nodeps
echo.
echo [提示] 当前解释器缺少核心依赖:
echo        %PYEXE%
echo.
echo 请先运行环境配置脚本（自动创建 .venv312 并安装依赖）:
echo        setup_venv.bat
echo.
echo 或手动安装:
echo        pip install -r requirements.txt
echo.
pause
exit /b 1

:nopy
echo.
echo [错误] 未找到 Python 解释器。
echo.
echo 请先安装 Python 3.10 或更高版本（安装时勾选 Add Python to PATH）:
echo        https://www.python.org/downloads/
echo.
pause
exit /b 1
