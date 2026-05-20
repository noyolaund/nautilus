@echo off
REM ===========================================================================
REM  SETUP — install and validate all dependencies
REM ===========================================================================
REM  Run this ONCE before using start.bat.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo ===========================================================================
echo   JDE Automation - Dependency Setup
echo ===========================================================================
echo.

REM --- 1. Verify Python is available -----------------------------------------
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Python is not installed or not on PATH.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   and make sure "Add Python to PATH" is checked.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo   Found %%v

REM --- 2. Upgrade pip --------------------------------------------------------
echo.
echo [2/4] Upgrading pip...
python -m pip install --upgrade pip
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
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   ERROR: failed to install one or more packages.
    pause
    exit /b 1
)

REM --- 4. Install the Chromium browser for Playwright ------------------------
echo.
echo [4/4] Installing Chromium browser for Playwright...
python -m playwright install chromium
if errorlevel 1 (
    echo.
    echo   ERROR: failed to install Chromium.
    pause
    exit /b 1
)

REM --- Validate core imports -------------------------------------------------
echo.
echo Validating installed packages...
python -c "import fastapi, uvicorn, playwright, openpyxl, httpx, pydantic, dotenv; print('  All core packages import OK')"
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

echo.
echo ===========================================================================
echo   SETUP COMPLETE
echo ===========================================================================
echo   You can now run:  start.bat
echo ===========================================================================
echo.
pause
endlocal
