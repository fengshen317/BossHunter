@echo off
setlocal
cd /d "%~dp0"
docker compose --env-file .env.docker down
set "CONNECTOR=%~dp0dist\connector\bosshunter-browser-connector.exe"
if exist "%CONNECTOR%" "%CONNECTOR%" stop
echo [BossHunter] 服务已停止；数据库卷、浏览器和登录 Profile 均已保留。
