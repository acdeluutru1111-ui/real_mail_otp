@echo off
REM Helper launched in its own window by run-demo.bat (cwd = backend\).
REM Uses the system Python (deps already installed). No venv needed.
title real_mail_otp BACKEND (port 8099)
echo.
echo === Backend API: http://127.0.0.1:8099    (docs: /docs) ===
echo === Press Ctrl+C to stop ===
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --reload
echo.
echo Backend stopped. Press any key to close this window.
pause >nul
