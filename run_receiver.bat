@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 goto nopython

if not exist "D:\Sarthi\multipart_buffer" mkdir "D:\Sarthi\multipart_buffer"

python -c "import win32com.client" >nul 2>&1
if errorlevel 1 (
  echo Installing pywin32 ...
  python -m pip install pywin32
)

echo.
echo Starting Sarthi Receiver - polling Outlook every 60 seconds.
echo Leave this window open. Close it or press Ctrl+C to stop.
echo.
python sarthi_receiver.py --watch --interval 60
goto end

:nopython
echo.
echo Python was not found on PATH.
echo Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
echo.

:end
pause
