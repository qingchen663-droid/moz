@echo off
setlocal
title moz

rem One-click moz launcher: starts backend/frontend only if they are not
rem already running, then opens the UI in a frameless app window.
rem NOTE: keep this file ASCII-only - cmd.exe parses .bat with the OEM
rem codepage and UTF-8 Chinese text breaks command parsing.

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "APP_URL=http://127.0.0.1:3000"

if not exist "%PYTHON%" (
    echo [moz] .venv not found. Run start.bat once first.
    pause
    exit /b 1
)

netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 goto frontend_check
echo [moz] starting backend on 8000 ...
start "moz Backend" /min /d "%ROOT%backend" cmd /k "%PYTHON%" -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload

:frontend_check
netstat -ano | findstr /r /c:":3000 .*LISTENING" >nul 2>&1
if %errorlevel% equ 0 goto wait_ready
echo [moz] starting frontend on 3000 ...
start "moz Frontend" /min /d "%ROOT%frontend" cmd /k npm run dev

:wait_ready
set "TRIES=0"

:wait_loop
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%APP_URL%' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 goto open_app
set /a TRIES+=1
if %TRIES% geq 40 goto timeout
timeout /t 1 /nobreak >nul
goto wait_loop

:timeout
echo [moz] frontend did not come up in 40s. Check the "moz Frontend" window.
pause
exit /b 1

:open_app
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"

if defined BROWSER (
    start "" "%BROWSER%" --app=%APP_URL% --window-size=1180,820 --window-position=90,60
) else (
    echo [moz] Edge/Chrome not found, opening with the default browser.
    start "" "%APP_URL%"
)

rem Tray keeps proactive care notifications alive even when the page is closed.
rem Safe to launch twice: tray_app.py exits itself if another instance holds the mutex.
if exist "%ROOT%.venv\Scripts\pythonw.exe" (
    start "" /min "%ROOT%.venv\Scripts\pythonw.exe" "%ROOT%backend\tray_app.py"
) else (
    start "" /min "%PYTHON%" "%ROOT%backend\tray_app.py"
)
exit /b 0
