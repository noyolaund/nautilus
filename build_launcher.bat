@echo off
REM ===========================================================================
REM  Build the JDE Automation Launcher into a standalone .exe
REM ===========================================================================
REM  Output: dist\JDE-Automation-Launcher.exe
REM ===========================================================================

echo.
echo  Building JDE Automation Launcher...
echo.

REM 1. Make sure PyInstaller is installed
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    python -m pip install pyinstaller
)

REM 2. Clean previous builds
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM 3. Build using the spec
python -m PyInstaller launcher.spec --noconfirm

if errorlevel 1 (
    echo.
    echo  BUILD FAILED — see the errors above.
    pause
    exit /b 1
)

echo.
echo ===========================================================================
echo  BUILD COMPLETE
echo ===========================================================================
echo  Executable: dist\JDE-Automation-Launcher.exe
echo.
echo  Before running on a fresh machine:
echo    1. Copy your .env file next to the .exe
echo    2. Run once:  playwright install chromium
echo ===========================================================================
echo.
pause
