@echo off
REM ===========================================================================
REM  SETUP — install and validate all dependencies
REM ===========================================================================
REM  Run this ONCE before using start.bat.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ===========================================================================
echo   JDE Automation - Dependency Setup
echo ===========================================================================
echo.

REM --- 1. Detect Python and pick the LATEST installed version ----------------
echo [1/4] Detecting Python installations...
echo.

set "PY_CMD="

REM First try the Windows `py` launcher — it knows about every installed Python
py --version >nul 2>&1
if not errorlevel 1 (
    echo   Installed Python versions on this machine:
    echo   ----------------------------------------------------------------
    REM `py --list` is the modern listing; `py -0` works on older launchers
    py --list 2>nul
    if errorlevel 1 (
        py -0 2>nul
    )
    echo   ----------------------------------------------------------------
    echo.

    REM `py -3` auto-selects the highest-numbered Python 3.x that's installed
    set "PY_CMD=py -3"
    for /f "delims=" %%v in ('py -3 --version 2^>^&1') do (
        echo   Selected latest: %%v
    )
) else (
    REM Fallback: plain `python`
    python --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   ERROR: Neither the `py` launcher nor `python` is on PATH.
        echo   Install Python 3.10+ from https://www.python.org/downloads/
        echo   and make sure "Add Python to PATH" is checked during install.
        echo.
        pause
        exit /b 1
    )
    set "PY_CMD=python"
    for /f "delims=" %%v in ('python --version 2^>^&1') do (
        echo   Found %%v  ^(`py` launcher not available, using plain `python`^)
    )
)

echo   Command to use: !PY_CMD!
echo.

REM --- 2. Upgrade pip --------------------------------------------------------
echo [2/4] Upgrading pip...
!PY_CMD! -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo   ERROR: failed to upgrade pip.
    pause
    exit /b 1
)

REM --- 3. Install requirements.txt -------------------------------------------
echo.
echo [3/4] Installing Python packages from requirements.txt...
if not exist requirements.txt (
    echo   ERROR: requirements.txt not found in %CD%
    pause
    exit /b 1
)
!PY_CMD! -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   ERROR: failed to install one or more packages.
    pause
    exit /b 1
)

REM --- 4. Install the Chromium browser for Playwright ------------------------
echo.
echo [4/4] Installing Chromium browser for Playwright...
!PY_CMD! -m playwright install chromium
if errorlevel 1 (
    echo.
    echo   ERROR: failed to install Chromium.
    pause
    exit /b 1
)

REM --- Validate core imports -------------------------------------------------
echo.
echo Validating installed packages...
!PY_CMD! -c "import fastapi, uvicorn, playwright, openpyxl, httpx, pydantic, dotenv; print('  All core packages import OK')"
if errorlevel 1 (
    echo.
    echo   ERROR: validation failed - some packages did not import.
    pause
    exit /b 1
)

REM --- Check .env exists -----------------------------------------------------
echo.
if exist .env (
    echo   .env file found.
) else (
    echo   WARNING: .env not found. Copy .env.example to .env and fill in values:
    echo            copy .env.example .env
)

REM --- Persist the chosen Python command for start.bat -----------------------
echo !PY_CMD!> .python_cmd

echo.
echo ===========================================================================
echo   SETUP COMPLETE
echo ===========================================================================
echo   Python used: !PY_CMD!
echo   You can now run:  start.bat
echo ===========================================================================
echo.
pause
endlocal
