@echo off
REM ============================================================
REM  Update the Sarthi Dump Processor from GitHub (no git needed).
REM  Downloads the latest code over HTTPS, same as PMD.
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install from https://www.python.org/downloads/ (tick Add to PATH).
  pause & exit /b 1
)

python updater.py "%~dp0."

echo.
echo ============================================================
echo Update complete. Restart the app so the new code loads:
echo   1) close/Ctrl+C the app or receiver window
echo   2) run run_app.bat  (or run_receiver.bat)
echo ============================================================
pause
