@echo off
setlocal
cd /d "%~dp0\..\.."

if not exist "bin\\grvt-transfer-cli.exe" (
  echo bin\grvt-transfer-cli.exe not found.
  echo If you downloaded a release zip, run Start.bat from inside the unzipped folder.
  exit /b 1
)

echo Starting grvt-transfer...
start "grvt-transfer" /b "%~dp0\..\..\bin\grvt-transfer-cli.exe" run
echo Started.
