@echo off
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "NO_PAUSE=0"
set "PS_ARGS="

:parse_args
if "%~1"=="" goto run_ps
if /I "%~1"=="--no-pause" (
  set "NO_PAUSE=1"
) else (
  set "PS_ARGS=!PS_ARGS! "%~1""
)
shift
goto parse_args

:run_ps
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start.ps1" !PS_ARGS!
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Start failed, exit code=%EXIT_CODE%
)
if not "%NO_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
