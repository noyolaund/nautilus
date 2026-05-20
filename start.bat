@echo off
REM ===========================================================================
REM  START — launch the LLM proxy and the dashboard in separate consoles
REM ===========================================================================
REM  Run setup.bat once before using this.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo ===========================================================================
echo   JDE Automation - Start
echo ===========================================================================
echo.
echo   Select the LLM proxy to use:
echo.
echo     [1] proxy      - Globant GeAI
echo     [2] proxy-jnj  - JNJ Azure OpenAI (Cloud PC, needs VPN)
echo.

choice /c 12 /n /m "  Enter 1 or 2: "
REM choice sets errorlevel: 1 for the first option, 2 for the second.
REM errorlevel checks are >=, so test the higher value first.
if errorlevel 2 (
    set "PROXY_CMD=proxy-jnj"
    set "PROXY_PORT=3457"
    set "PROXY_NAME=JNJ Azure Proxy"
) else (
    set "PROXY_CMD=proxy"
    set "PROXY_PORT=3456"
    set "PROXY_NAME=Globant GeAI Proxy"
)

REM Point the dashboard's engines at the chosen proxy.
REM The dashboard console inherits this environment variable.
set "STAGEHAND_SERVER_URL=http://localhost:%PROXY_PORT%"

echo.
echo   Proxy:     %PROXY_NAME%  (port %PROXY_PORT%)
echo   Dashboard: http://localhost:5000
echo.
echo   Opening two new console windows...
echo.

REM --- Start the proxy in its own console ------------------------------------
start "JDE %PROXY_NAME%" cmd /k python main.py %PROXY_CMD%

REM --- Give the proxy a few seconds to come up -------------------------------
echo   Waiting for the proxy to start...
timeout /t 5 /nobreak >nul

REM --- Start the dashboard in its own console --------------------------------
start "JDE Dashboard" cmd /k python main.py dashboard

REM --- Give the dashboard a moment, then open the browser --------------------
echo   Waiting for the dashboard to start...
timeout /t 5 /nobreak >nul
start "" http://localhost:5000

echo.
echo ===========================================================================
echo   Both servers are running in their own windows.
echo     Proxy window:     "JDE %PROXY_NAME%"
echo     Dashboard window: "JDE Dashboard"
echo.
echo   Close those windows to stop the servers.
echo ===========================================================================
echo.
echo   This launcher window can be closed now.
pause
endlocal
