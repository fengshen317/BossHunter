@echo off
setlocal
cd /d "%~dp0"
docker compose --env-file .env.docker logs --tail 200 -f app postgres
