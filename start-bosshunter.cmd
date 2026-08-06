@echo off
setlocal
cd /d "%~dp0"

echo Starting BossHunter dashboard and browser...
if exist ".venv\Scripts\bosshunter.exe" (
    ".venv\Scripts\bosshunter.exe" start %*
) else (
    set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
    python -m bosshunter start %*
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
