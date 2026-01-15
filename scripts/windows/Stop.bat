@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "rebalance_loop.pid" (
  echo rebalance_loop.pid not found. Nothing to stop?
  exit /b 0
)

set /p PID=<rebalance_loop.pid
if "%PID%"=="" (
  echo Empty PID file.
  del /q rebalance_loop.pid >nul 2>&1
  exit /b 0
)

echo Stopping PID %PID% ...
taskkill /PID %PID% /T /F >nul 2>&1
del /q rebalance_loop.pid >nul 2>&1
echo Stopped.

