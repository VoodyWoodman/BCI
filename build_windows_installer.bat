@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Keep the console visible when launched by double-clicking.
set "PAUSE_AT_END=1"
if /I "%~1"=="--no-pause" set "PAUSE_AT_END=0"

set "RESULT=1"
echo ============================================================
set "BCI_VERSION=0.5.2-alpha"
echo BCI %BCI_VERSION% - Windows x64 installer build
echo ============================================================

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
echo Installing build and test dependencies...
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements-windows.txt
if errorlevel 1 goto :error
python -m pip install -r requirements-test.txt
if errorlevel 1 goto :error

echo.
echo Running pytest...
python -m pytest -q
if errorlevel 1 goto :error
echo.
echo Running unittest...
python -m unittest discover -s tests -p "test*.py" -q
if errorlevel 1 goto :error

if not exist "redist" mkdir "redist"
echo.
echo Downloading Microsoft Visual C++ Redistributable...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile 'redist\vc_redist.x64.exe'"
if errorlevel 1 goto :error
if not exist "redist\vc_redist.x64.exe" (
  echo ERROR: VC++ Redistributable download did not produce the expected file.
  goto :error
)

rem Build the same portable onedir folder that is also included in the installer.
echo.
echo Building portable application folder...
call build_windows_exe.bat --prepared --no-pause
if errorlevel 1 goto :error

set "ISCC_EXE="
where ISCC.exe >nul 2>nul
if not errorlevel 1 set "ISCC_EXE=ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
  echo ERROR: Inno Setup 6 was not found.
  echo The portable folder is available at dist\BCI if its build succeeded.
  goto :error
)

echo.
echo Building Windows installer...
"%ISCC_EXE%" /Qp /DMyAppVersion=%BCI_VERSION% BCI_Setup.iss
if errorlevel 1 goto :error
if not exist "dist\BCI_Setup_%BCI_VERSION%_x64.exe" (
  echo ERROR: Inno Setup finished without creating the expected installer.
  goto :error
)

echo.
echo ============================================================
echo BUILD SUCCESSFUL
echo Portable folder: %CD%\dist\BCI
echo Installer: %CD%\dist\BCI_Setup_%BCI_VERSION%_x64.exe
echo End users do not need Python, pip, or Inno Setup.
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
echo INSTALLER BUILD FAILED. Read the error messages above.
echo The installer was NOT created unless explicitly shown above.
echo ============================================================
set "RESULT=1"

:finish
echo.
if "%PAUSE_AT_END%"=="1" (
  echo Press any key to close this window...
  pause >nul
)
endlocal & exit /b %RESULT%
