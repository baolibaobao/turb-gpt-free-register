@echo off
setlocal

cd /d "%~dp0"
set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"
set "PROJECT_HOST=127.0.0.1"
set "PROJECT_PORT=5000"
set "PROJECT_URL=http://%PROJECT_HOST%:%PROJECT_PORT%/login"

if not exist "%PROJECT_PYTHON%" (
    echo [ERROR] Project virtual environment was not found:
    echo %PROJECT_PYTHON%
    echo.
    echo Please run: python -m venv .venv
    echo Then run: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PROJECT_PORT% .*LISTENING"') do set "PROJECT_RUNNING_PID=%%P"

if defined PROJECT_RUNNING_PID (
    echo WebUI is already running on port %PROJECT_PORT% ^(PID %PROJECT_RUNNING_PID%^).
    echo Opening %PROJECT_URL%
    start "" "%PROJECT_URL%"
    exit /b 0
)

echo Starting Turb GPT Free Register WebUI...
echo Project: %~dp0
echo Address: %PROJECT_URL%
echo.
echo Keep the server window open. Use the login code configured in .env.
echo Project subscriptions: http://%PROJECT_HOST%:%PROJECT_PORT%/proxy-subscription

start "Turb GPT Free Register WebUI" cmd /k ""%PROJECT_PYTHON%" "%~dp0web.py" --host %PROJECT_HOST% --port %PROJECT_PORT% --open-browser"

echo Browser will open when the server starts: %PROJECT_URL%
exit /b 0
