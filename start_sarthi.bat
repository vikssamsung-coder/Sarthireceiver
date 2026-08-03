@echo off
REM ============================================================
REM  start_sarthi.bat -- the ONE file to double-click every day.
REM  It always runs Sarthi from the folder containing this BAT.
REM ============================================================
cd /d "%~dp0"
title Sarthi

if not exist "run_app.bat" (
  echo ERROR: run_app.bat was not found in:
  echo %CD%
  echo.
  echo Keep start_sarthi.bat inside the Sarthireceiver folder.
  pause
  exit /b 1
)

call "%~dp0run_app.bat"
