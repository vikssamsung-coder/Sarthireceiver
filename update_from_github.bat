@echo off
REM ============================================================
REM  Update the Sarthi Dump Processor from GitHub on this box.
REM  Run from the app folder. Does the same thing as the app's
REM  Settings -> Update button.
REM ============================================================
setlocal
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed. Get it from https://git-scm.com/download/win
  pause & exit /b 1
)

if not exist ".git" (
  echo First-time setup: linking this folder to GitHub...
  git init
  git remote add origin https://github.com/vikssamsung-coder/Sarthireceiver.git
  git fetch origin
  git checkout -f main
) else (
  git pull origin main
)

echo.
echo ============================================================
echo Update complete. Restart the app so the new code loads:
echo   1) press Ctrl+C in the Streamlit window
echo   2) run:  streamlit run app.py
echo ============================================================
pause
