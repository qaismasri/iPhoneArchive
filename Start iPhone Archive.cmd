@echo off
where python >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 was not found on PATH.
    pause
    exit /b 1
)
python -B "%~dp0app.py"
if errorlevel 1 pause
