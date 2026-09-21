@echo off
setlocal
cd /d "%~dp0"

rem ==============================================================
rem  BossHunter one-key start
rem    browser (isolated profile + remote debugging port)
rem    + local dashboard on http://127.0.0.1:8686
rem
rem  This script does NOT open the BOSS job site for you. Open
rem  www.zhipin.com in the launched browser and sign in; the session
rem  is kept in browser.user_data_dir for next time.
rem
rem  Messages are intentionally ASCII: cmd.exe renders non-ASCII
rem  text using the OEM code page, and a UTF-8 BOM breaks the very
rem  first line. Keeping this file ASCII avoids both failure modes.
rem ==============================================================

set "BOSSHUNTER_PORT=8686"

rem ---- port guard -------------------------------------------------
rem  Windows SO_REUSEADDR lets a SECOND instance bind a port that is
rem  already being listened on. That produces a "looks started, but no
rem  request is ever logged and the page does not respond" zombie. So
rem  check the port first and refuse instead of creating one.
set "PORT_BUSY="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /R /C:":%BOSSHUNTER_PORT% " ^| findstr /C:"LISTENING"') do set "PORT_BUSY=1"

if defined PORT_BUSY (
	echo.
	echo [!] Port %BOSSHUNTER_PORT% is already in use - BossHunter is probably running.
	echo     Check http://127.0.0.1:%BOSSHUNTER_PORT% first.
	echo     To restart: close the existing BossHunter console window,
	echo     or end python.exe in Task Manager.
	echo.
	pause
	exit /b 1
)

echo Starting BossHunter: browser + debug port + dashboard ...
set "BOSSHUNTER_BASE_DIR=%CD%"

if exist ".venv\Scripts\bosshunter.exe" (
	".venv\Scripts\bosshunter.exe" start --port %BOSSHUNTER_PORT%
) else (
	set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
	python -m bosshunter.main start --port %BOSSHUNTER_PORT%
)
set "BOSSHUNTER_EXIT_CODE=%ERRORLEVEL%"

if not "%BOSSHUNTER_EXIT_CODE%"=="0" (
	echo.
	echo BossHunter failed to start. Review the error above.
	echo Common causes: no Chrome/Edge installed, or browser.executable_path misconfigured.
)

if "%BOSSHUNTER_EXIT_CODE%"=="0" (
	echo.
	echo BossHunter has stopped.
)

pause
exit /b %BOSSHUNTER_EXIT_CODE%
