@echo off
setlocal
cd /d "%~dp0"
echo Setup started. Please wait...
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install Python 3 from https://www.python.org/downloads/windows/
  echo IMPORTANT: check "Add python.exe to PATH" during install.
  pause
  exit /b 1
)
echo [1/3] Upgrade pip...
python -m pip install --upgrade pip
echo [2/3] Install Python packages...
python -m pip install -r requirements.txt
echo [3/3] Install Playwright Chromium...
python -m playwright install chromium
echo Setup completed.
pause
