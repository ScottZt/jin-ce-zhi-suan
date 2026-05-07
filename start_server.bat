@echo off
echo ==========================================
echo JinCe ZhiSuan - Server Startup
echo ==========================================
echo.

REM Set Python environment
set "PYTHON_PATH=C:\Users\Administrator.DESKTOP-DK7FP95\AppData\Local\Programs\Python\Python311"
set "PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%PATH%"

cd /d D:\jin-ce-zhi-suan

echo [Step 1] Checking Python...
python --version
if errorlevel 1 (
    echo ERROR: Python not found at %PYTHON_PATH%
    pause
    exit /b 1
)

echo.
echo [Step 2] Installing dependencies...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo WARNING: Some dependencies may be missing
)

echo.
echo [Step 3] Starting server...
echo Server will run on http://localhost:8000
echo.
echo Press Ctrl+C to stop
echo.

python server.py

echo.
echo ==========================================
echo Server stopped
echo ==========================================
echo.
pause
