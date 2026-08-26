@echo off
REM Helper launched in its own window by run-demo.bat (cwd = frontend\).
title real_mail_otp FRONTEND (port 5173)
echo.
echo === Frontend SPA: http://localhost:5173 ===
echo === Press Ctrl+C to stop ===
echo.
call npm run dev -- --port 5173 --strictPort
echo.
echo Frontend stopped. Press any key to close this window.
pause >nul
