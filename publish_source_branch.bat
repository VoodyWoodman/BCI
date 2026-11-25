@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo BCI 0.5.2-alpha - publish source to GitHub branch
echo ============================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_source_branch.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Source sync failed. Read the error above.
if "%RESULT%"=="0" echo Source sync completed or was cancelled safely.
echo.
pause
endlocal & exit /b %RESULT%
