@echo off
REM run_sarthi.bat - ONE window: Outlook receiver + MIS poller.
REM goto labels, never (cmd & cmd).
cd /d D:\dump_processor_app

if not exist sarthi_service.py goto nofile
goto run

:nofile
echo sarthi_service.py not found in %CD%
pause
exit /b 1

:run
title Sarthi Service
python sarthi_service.py
if errorlevel 1 goto failed
goto done

:failed
echo.
echo Sarthi service exited with an error.
pause
exit /b 1

:done
echo Sarthi service stopped.
pause
