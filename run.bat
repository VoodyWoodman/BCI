@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 bci_app.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python bci_app.py
  ) else (
    echo Python 3 was not found.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
    echo During installation, enable "Add Python to PATH".
    pause
  )
)
endlocal
