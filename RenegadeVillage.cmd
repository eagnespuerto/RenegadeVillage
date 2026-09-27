@echo off
rem Double-click to open RenegadeVillage in its own window.
cd /d "%~dp0desktop"
if not exist node_modules\electron\dist\electron.exe (
  echo First run: installing the desktop shell...
  call npm install --no-fund --no-audit || (pause & exit /b 1)
)
start "" "%~dp0desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop"
