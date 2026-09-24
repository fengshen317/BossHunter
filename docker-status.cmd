@echo off
setlocal
cd /d "%~dp0"
docker compose --env-file .env.docker ps
set "CONNECTOR=%~dp0dist\connector\bosshunter-browser-connector.exe"
if exist "%CONNECTOR%" "%CONNECTOR%" health
