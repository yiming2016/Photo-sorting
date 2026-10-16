@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set PYTHON=python
where python >nul 2>&1
if errorlevel 1 (
    set PYTHON=py
)
"%PYTHON%" run.py
if errorlevel 1 (
    echo.
    echo 启动失败。请确认已安装 Python 3.10+ 和依赖：
    echo   pip install PySide6 Pillow
    echo.
    pause
)
