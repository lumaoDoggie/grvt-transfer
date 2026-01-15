@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "grvt-transfer-gui.exe" (
  echo grvt-transfer-gui.exe not found next to this script.
  echo If you downloaded a release zip, run Start-GUI.bat from inside the unzipped folder.
  exit /b 1
)

echo Starting GUI...
start "grvt-transfer-gui" "%~dp0\..\..\grvt-transfer-gui.exe"
echo Started.

