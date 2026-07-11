@echo off
REM ============================================================
REM  Start the Sarthi Receiver (reads Outlook, processes dumps).
REM  Keeps polling the inbox. Close this window to stop.
REM  Put it in the app folder and double-click.
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install from https://www.python.org/downloads/ (tick Add to PATH).
  pause & exit /b 1
)

if not exist "D:\Sarthi\multipart_buffer" mkdir "D:\Sarthi\multipart_buffer"

REM Outlook automation needs pywin32
python -c "import win32com.client" 2>nul
if errorlevel 1 (
  echo First run - installing pywin32...
  python -m pip install pywin32
)

echo Starting Sarthi Receiver (polling every 60s). Leave this window open.
python sarthi_receiver.py --watch --interval 60

pause
