@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "BCI_VERSION=0.5.2-alpha"

rem Keep the console visible when launched by double-clicking.
set "PAUSE_AT_END=1"
set "RESULT=1"
if /I "%~2"=="--no-pause" set "PAUSE_AT_END=0"

if /I "%~1"=="--prepared" goto :prepared
call :detect_python
if errorlevel 1 goto :error

set "BUILD_VENV=.build-venv-%PYTHON_VERSION%"
if not exist "%BUILD_VENV%\Scripts\python.exe" (
  echo.
  echo Creating build environment with Python %PYTHON_VERSION% x64...
  %PYTHON_CMD% -m venv "%BUILD_VENV%"
  if errorlevel 1 goto :error
)
call "%BUILD_VENV%\Scripts\activate.bat"
if errorlevel 1 goto :error

echo.
echo Installing Windows build dependencies...
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements-windows.txt
if errorlevel 1 goto :error
goto :build

:prepared
if not defined BUILD_VENV (
  if defined PYTHON_VERSION set "BUILD_VENV=.build-venv-%PYTHON_VERSION%"
)
if not defined BUILD_VENV (
  echo ERROR: The installer did not pass a prepared build environment.
  goto :error
)
if not exist "%BUILD_VENV%\Scripts\python.exe" (
  echo ERROR: Prepared Python build environment was not found: %BUILD_VENV%
  goto :error
)
call "%BUILD_VENV%\Scripts\activate.bat"
if errorlevel 1 goto :error

:build
echo.
echo Building portable BCI application...
if exist "build\BCI" rmdir /s /q "build\BCI"
if exist "dist\BCI" rmdir /s /q "dist\BCI"
python -m PyInstaller --noconfirm --clean --windowed --onedir --name BCI --distpath dist --workpath build --collect-all lightgbm --collect-binaries lightgbm --collect-all numpy --collect-all scipy bci_app.py
if errorlevel 1 goto :error
python copy_third_party_licenses.py "dist\BCI\_licenses"
if errorlevel 1 goto :error
(
  echo Bannerlord Compatibility Intelligence %BCI_VERSION% - Portable build
  echo.
  echo Run BCI.exe from this folder. Keep the _internal folder beside BCI.exe.
  echo This portable folder can be moved as a whole; do not move BCI.exe alone.
  echo User reports, database and baseline data are stored in the user's local application data folder.
) > "dist\BCI\README_PORTABLE.txt"
if errorlevel 1 goto :error

echo.
echo ============================================================
echo PORTABLE BUILD SUCCESSFUL
echo Folder: %CD%\dist\BCI
echo Executable: %CD%\dist\BCI\BCI.exe
echo ============================================================
set "RESULT=0"
goto :finish

:detect_python
set "PYTHON_CMD="
set "PYTHON_VERSION="
where py >nul 2>nul
if not errorlevel 1 (
  call :probe_runtime "py -3.12" 3.12
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
    set "PYTHON_VERSION=3.12"
  )
  if not defined PYTHON_CMD (
    call :probe_runtime "py -3.13" 3.13
    if not errorlevel 1 (
      set "PYTHON_CMD=py -3.13"
      set "PYTHON_VERSION=3.13"
    )
  )
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    call :probe_runtime "python" 3.12
    if not errorlevel 1 (
      set "PYTHON_CMD=python"
      set "PYTHON_VERSION=3.12"
    )
    if not defined PYTHON_CMD (
      call :probe_runtime "python" 3.13
      if not errorlevel 1 (
        set "PYTHON_CMD=python"
        set "PYTHON_VERSION=3.13"
      )
    )
  )
)
if not defined PYTHON_CMD (
  echo ERROR: This computer does not have a supported local build runtime.
  echo Local builds require Python 3.12 x64 or Python 3.13 x64.
  echo.
  echo This is NOT required to install or use BCI. Do not install Python just for end-user setup.
  echo.
  echo To create the official Windows installer without local Python:
  echo   1. Make sure Git for Windows is installed and authenticated with GitHub.
  echo   2. Run publish_source_branch.bat from this project folder.
  echo   3. GitHub Actions will build BCI on a Windows runner with Python 3.12.
  echo   4. Download the resulting installer from the repository Actions artifact.
  echo.
  echo Available Python runtimes reported by the Python Launcher:
  py -0p 2>nul
  exit /b 1
)
echo Build runtime selected: Python %PYTHON_VERSION% x64
%PYTHON_CMD% --version
if errorlevel 1 (
  echo ERROR: The selected Python runtime could not be started after preflight.
  exit /b 1
)
exit /b 0

rem The Python install manager may emit a missing-runtime message without a
rem dependable nonzero exit code. Accept a runtime only when a real interpreter
rem prints the exact sentinel after version and 64-bit checks have passed.
:probe_runtime
set "PROBE_CMD=%~1"
set "PROBE_VERSION=%~2"
set "PROBE_MAJOR=%PROBE_VERSION:~0,1%"
set "PROBE_MINOR=%PROBE_VERSION:~2,2%"
set "PROBE_FILE=%TEMP%\bci-runtime-probe-%RANDOM%-%RANDOM%.txt"
set "PROBE_RESULT=1"
%PROBE_CMD% -c "import sys; print('BCI_RUNTIME_OK' if sys.version_info[:2] == (%PROBE_MAJOR%, %PROBE_MINOR%) and sys.maxsize > 2**32 else 'BCI_RUNTIME_INVALID')" > "%PROBE_FILE%" 2>nul
if exist "%PROBE_FILE%" (
  findstr /X /C:"BCI_RUNTIME_OK" "%PROBE_FILE%" >nul 2>nul
  if not errorlevel 1 set "PROBE_RESULT=0"
  del /q "%PROBE_FILE%" >nul 2>nul
)
if "%PROBE_RESULT%"=="0" exit /b 0
exit /b 1
:error
echo.
echo ============================================================
echo PORTABLE BUILD FAILED. Read the error messages above.
echo No successful build is claimed.
echo ============================================================
set "RESULT=1"

:finish
echo.
if "%PAUSE_AT_END%"=="1" (
  echo Press any key to close this window...
  pause >nul
)
endlocal & exit /b %RESULT%
