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

REM install dependencies the first time
python -m streamlit version >nul 2>nul
if errorlevel 1 (
  echo First run - installing dependencies...
  python -m pip install --upgrade pip
  python -m pip install streamlit pandas "psycopg[binary]"
)

echo Starting the app... a browser tab will open.
echo Leave this window open while you use it. Close it (or Ctrl+C) to stop.
python -m streamlit run app.py

pause
