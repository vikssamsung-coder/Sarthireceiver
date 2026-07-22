@echo off
REM ============================================================
REM  Start the Sarthi Dump Processor app.
REM  Put this in the same folder as app.py and double-click it.
REM ============================================================
cd /d "%~dp0"

REM python present?
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install Python 3 from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
  pause & exit /b 1
)

REM make sure the registry folder exists
if not exist "D:\Sarthi\multipart_buffer" mkdir "D:\Sarthi\multipart_buffer"

REM install the complete dependency set when anything is missing
python -c "import streamlit,pandas,openpyxl,psutil,psycopg,win32com.client" >nul 2>nul
if errorlevel 1 (
  echo Installing missing dependencies...
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed.
    pause & exit /b 1
  )
)

echo Starting the app... a browser tab will open.
echo Leave this window open while you use it. Close it (or Ctrl+C) to stop.
python -m streamlit run app.py

pause
