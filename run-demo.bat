@echo off
setlocal enabledelayedexpansion
title real_mail_otp - Demo launcher

REM ============================================================
REM  real_mail_otp local demo launcher
REM  Backend : http://127.0.0.1:8099   (FastAPI, docs at /docs)
REM  Frontend: http://localhost:5173   (Vite dev server)
REM  Port 8080 is intentionally NOT used.
REM ============================================================

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"

echo.
echo ============================================================
echo   real_mail_otp - starting local demo
echo   Backend : http://127.0.0.1:8099  (docs: /docs)
echo   Frontend: http://localhost:5173
echo   (Port 8080 is not used)
echo ============================================================
echo.

REM --- 0) Tooling checks --------------------------------------
where python >nul 2>&1 || (echo [ERROR] Python khong tim thay trong PATH. & goto :fail)
where node   >nul 2>&1 || (echo [ERROR] Node.js khong tim thay trong PATH. & goto :fail)
where npm    >nul 2>&1 || (echo [ERROR] npm khong tim thay trong PATH. & goto :fail)

REM --- 1) Backend dependencies (system Python, no venv) -------
cd /d "%BACKEND%" || (echo [ERROR] Khong vao duoc thu muc backend. & goto :fail)
echo [backend] Kiem tra dependencies ...
python -c "import fastapi,uvicorn,asyncpg,alembic,sqlalchemy,passlib,bcrypt,jose,cryptography,pydantic_settings,httpx" 1>nul 2>nul
if errorlevel 1 (
    echo [backend] Thieu dependencies, dang cai tu requirements.txt ... co the mat 1-2 phut
    python -m pip install --disable-pip-version-check -r requirements.txt || (echo [ERROR] pip install that bai. & goto :fail)
) else (
    echo [backend] Dependencies da co san.
)

REM --- 2) .env presence + DATABASE_URL sanity -----------------
if not exist ".env" (
    echo [backend] Khong thay .env, tao tu .env.example ...
    copy /Y ".env.example" ".env" >nul
)
findstr /C:"ep-xxx.neon.tech" ".env" >nul 2>&1
if !errorlevel!==0 (
    echo.
    echo ============================================================
    echo   [CAN CAU HINH DATABASE]
    echo   backend\.env dang dung DATABASE_URL mau - chua that.
    echo   Backend BAT BUOC dung PostgreSQL ^(khong ho tro SQLite^).
    echo   Cach nhanh nhat: tao DB mien phi tren https://neon.tech
    echo   roi dan chuoi ket noi vao dong DATABASE_URL trong:
    echo       %BACKEND%\.env
    echo   Sau do chay lai run-demo.bat.
    echo ============================================================
    echo.
    echo Dang mo backend\.env de ban chinh sua...
    start "" notepad "%BACKEND%\.env"
    goto :fail
)

REM --- 3) Database migrations ---------------------------------
echo [backend] Chay migrations (alembic upgrade head) ...
alembic upgrade head || (echo [ERROR] Migration that bai. Kiem tra DATABASE_URL trong backend\.env. & goto :fail)

REM --- 4) Frontend dependencies -------------------------------
cd /d "%FRONTEND%" || (echo [ERROR] Khong vao duoc thu muc frontend. & goto :fail)
if not exist "node_modules" (
    echo [frontend] Cai dependencies npm install ... co the mat 1-2 phut
    call npm install || (echo [ERROR] npm install that bai. & goto :fail)
)

REM --- 4.5) Free ports 8099 / 5173 if still held --------------
echo [ports] Giai phong cong 8099 va 5173 neu dang bi chiem ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8099 " ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173 " ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1

REM --- 5) Launch both servers in their own windows ------------
echo [launch] Mo cua so Backend (port 8099) ...
start "real_mail_otp Backend" /D "%BACKEND%" cmd /k _run_backend.cmd

echo [launch] Mo cua so Frontend (port 5173) ...
start "real_mail_otp Frontend" /D "%FRONTEND%" cmd /k _run_frontend.cmd

REM --- 6) Open the app in the default browser -----------------
echo [launch] Doi frontend khoi dong roi mo trinh duyet ...
timeout /t 5 /nobreak >nul
start "" http://localhost:5173

echo.
echo ============================================================
echo   Da khoi dong. Hai cua so dang chay:
echo     - Backend : http://127.0.0.1:8099  (docs: /docs)
echo     - Frontend: http://localhost:5173
echo   Dong 2 cua so do de dung demo.
echo ============================================================
echo.
echo Nhan phim bat ky de dong cua so launcher nay.
pause >nul
exit /b 0

:fail
echo.
echo Demo KHONG khoi dong duoc. Xem thong bao loi o tren.
echo Nhan phim bat ky de dong.
pause >nul
exit /b 1
