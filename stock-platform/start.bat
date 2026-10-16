@echo off
REM ============================================================
REM  Quant platform launcher
REM  Keep this file ASCII-only: cmd parses .bat files in the
REM  ANSI codepage, so UTF-8 Chinese text breaks line parsing.
REM  (Run install.bat once first to install dependencies)
REM ============================================================
cd /d "%~dp0"

echo [1/2] Starting backend (port 8000)...
start "Backend" cmd /k "cd backend && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

REM wait for backend to come up
timeout /t 3 /nobreak > nul

echo [2/2] Starting frontend (port 3000)...
start "Frontend" cmd /k "cd frontend && npm run dev"

echo.
echo ==================================================
echo  Two service windows opened - keep them running.
echo  Close those windows to stop the services.
echo.
echo  Backend API docs: http://localhost:8000/docs
echo  Frontend UI:       http://localhost:3000
echo ==================================================
echo.
pause
