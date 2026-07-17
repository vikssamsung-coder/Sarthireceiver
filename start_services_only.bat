@echo off
REM ============================================================
REM  start_services_only.bat  --  receiver + MIS, NO app window.
REM  For an unattended box (Task Scheduler at boot) where nobody
REM  opens the Streamlit UI.
REM
REM  It refuses to start if the services are ALREADY running (e.g.
REM  the app started them), so you never get two receivers both
REM  polling the same inbox. goto labels, never (cmd & cmd).
REM ============================================================
cd /d D:\dump_processor_app

if not exist sarthi_service.py goto nofile

REM ask the shared PID lock whether services are already up
python -c "import service_manager as s; import sys; sys.exit(0 if s.status(force=True)['running'] else 1)"
if %errorlevel%==0 goto already
goto run

:nofile
echo sarthi_service.py not found in %CD%
pause
exit /b 1

:already
echo Services are ALREADY running (started by the app or another window).
echo Not starting a second copy. Stop them from the app's Services screen first.
pause
exit /b 0

:run
title Sarthi Services
echo Starting receiver + MIS poller (no app UI). Ctrl+C to stop.
echo.
python service_manager.py start
python -c "import service_manager as s; print('log:', s.LOGFILE)"
echo.
echo Services launched in the background. This window can be closed;
echo they keep running. Stop them with:  python service_manager.py stop
pause
goto done

:done
exit /b 0
