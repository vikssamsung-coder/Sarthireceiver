@echo off
REM ============================================================
REM  start_sarthi.bat  --  the ONE thing to launch.
REM  Opens the app; the app starts the receiver + MIS poller
REM  behind it (service_manager), so everything comes up together.
REM  goto labels, never (cmd & cmd).
REM ============================================================
cd /d D:\dump_processor_app

if not exist app.py goto noapp
goto run

:noapp
echo app.py not found in %CD%
echo Are the files in D:\dump_processor_app ?
pause
exit /b 1

:run
title Sarthi
echo Starting Sarthi (app + receiver + MIS)...
echo A browser tab will open. Closing THIS window stops the app,
echo but the receiver and MIS keep running in the background.
echo.
python -m streamlit run app.py
if errorlevel 1 goto failed
goto done

:failed
echo.
echo Streamlit exited with an error. If it says 'streamlit' is not
echo recognised, install it:  pip install streamlit
pause
exit /b 1

:done
echo App stopped.
pause
