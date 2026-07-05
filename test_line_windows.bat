@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo Sending LINE test message...
python sugoroku_checker.py --line-test
if errorlevel 1 (
  echo.
  echo LINE test failed. Check line_config.json.
)
pause
