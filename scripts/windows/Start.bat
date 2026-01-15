@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "grvt-transfer.exe" (
  echo grvt-transfer.exe not found next to this script.
  echo If you downloaded a release zip, run Start.bat from inside the unzipped folder.
  exit /b 1
)

echo Starting grvt-transfer...
start "grvt-transfer" /b "%~dp0\..\..\grvt-transfer.exe" run
echo Started.

