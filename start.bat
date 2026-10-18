@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Try: python server_desk.py
  pause
  exit /b 1
)
start "" pythonw "%~dp0widget.py"