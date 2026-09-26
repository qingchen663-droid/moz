@echo off
setlocal
title moz - AI Companion

echo ============================================
echo    moz - One-Click Start
echo ============================================
echo.

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".env" (
    echo [ERROR] .env not found. Run: copy .env.example .env
    pause
    exit /b 1
)

set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if exist "%PYTHON%" goto check_python

where py >nul 2>&1
if %errorlevel% neq 0 goto use_system_python
py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 13) else 1)" >nul 2>&1
if %errorlevel% neq 0 goto use_system_python

echo [1/4] Creating Python 3.13 virtual environment...
py -3.13 -m venv .venv
goto check_python

:use_system_python
where python >nul 2>&1
if %errorlevel% neq 0 goto no_python
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 goto unsupported_python
echo [1/4] Creating virtual environment with the system Python...
python -m venv .venv

:check_python
if not exist "%PYTHON%" goto venv_failed
"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 goto unsupported_python

echo       Checking backend dependencies...
"%PYTHON%" -c "import fastapi" >nul 2>&1
if %errorlevel% equ 0 goto install_frontend
echo       Installing backend dependencies...
"%PYTHON%" -m pip install -r requirements.txt
if %errorlevel% neq 0 goto backend_failed

:install_frontend
where node >nul 2>&1
if %errorlevel% neq 0 goto no_node

if exist "frontend\node_modules" goto start_services
echo [2/4] Installing frontend dependencies from package-lock.json...
pushd frontend
call npm ci --no-audit --no-fund
set "NPM_RESULT=%errorlevel%"
popd
if not "%NPM_RESULT%"=="0" goto frontend_failed

:start_services
echo [3/4] Starting backend on port 8000...
start "moz Backend" /d "%ROOT%backend" cmd /k "%PYTHON%" -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload

echo [4/4] Starting frontend on port 3000...
start "moz Frontend" /d "%ROOT%frontend" cmd /k npm run dev

echo.
echo ============================================
echo    Started!
echo    Backend:  http://127.0.0.1:8000
echo    Frontend: http://127.0.0.1:3000
echo ============================================
echo.
echo    Run as a desktop app (one time only):
echo      open the URL above, then click the install icon
echo      in the address bar (or menu ... - Apps - Install this site as an app).
echo      Afterwards launch "moz" from the Start menu / taskbar.
echo      Code edits still hot-reload in that window.
echo.
echo    Always use 127.0.0.1:3000 - "localhost:3000" is a different
echo    origin and has its own storage (avatar / access key).
echo.
echo Closing this window won't stop services.
echo Close the Backend/Frontend windows to stop.
start "" http://127.0.0.1:3000
pause
exit /b 0

:no_python
echo [ERROR] Python not found. Install Python 3.13 or later.
goto fail

:unsupported_python
echo [ERROR] The selected Python is older than 3.11. Install Python 3.13.
goto fail

:venv_failed
echo [ERROR] Failed to create .venv.
goto fail

:backend_failed
echo [ERROR] Failed to install backend dependencies.
goto fail

:no_node
echo [ERROR] Node.js not found. Install Node.js 22 LTS.
goto fail

:frontend_failed
echo [ERROR] Failed to install frontend dependencies.

:fail
pause
exit /b 1
