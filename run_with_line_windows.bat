@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo Running Sugoroku checker with LINE notification...
python sugoroku_checker.py --year 2026 --month 8 --day 19 --headless --notify-line
if errorlevel 1 (
  echo.
  echo Error occurred. Try run_debug_windows.bat and send run_log.txt.
)
pause
