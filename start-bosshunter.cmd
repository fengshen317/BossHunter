@echo off
setlocal
cd /d "%~dp0"

echo Starting BossHunter backend...
set "BOSSHUNTER_BASE_DIR=%CD%"
set "BOSSHUNTER_OPEN_BROWSER=false"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m bosshunter
) else (
    set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
    python -m bosshunter
)
set "BOSSHUNTER_EXIT_CODE=%ERRORLEVEL%"

if not "%BOSSHUNTER_EXIT_CODE%"=="0" (
    echo.
    echo BossHunter failed to start. Review the error above.
)

if "%BOSSHUNTER_EXIT_CODE%"=="0" (
    echo.
    echo BossHunter has stopped.
)

pause
exit /b %BOSSHUNTER_EXIT_CODE%
