@echo off
setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
  echo [BossHunter] 未找到 Docker，请先安装并启动 Docker Desktop。
  exit /b 1
)
docker info >nul 2>nul
if errorlevel 1 (
  echo [BossHunter] Docker Desktop 尚未启动。
  exit /b 1
)

set "CONNECTOR=%~dp0dist\connector\bosshunter-browser-connector.exe"
if not exist "%CONNECTOR%" (
  echo [BossHunter] 缺少 Browser Connector。
  echo 请下载并完整解压 Release ZIP，不要只下载源代码压缩包。
  exit /b 1
)

if not exist ".env.docker" "%CONNECTOR%" init
if errorlevel 1 exit /b 1

start "BossHunter Browser Connector" /min "%CONNECTOR%" start
for /l %%I in (1,1,30) do (
  timeout /t 1 /nobreak >nul
  "%CONNECTOR%" health >nul 2>nul && goto connector_ready
)
echo [BossHunter] Browser Connector 未就绪。
echo 如 9222 端口被占用，请在 .env.docker 中设置 BOSSHUNTER_CHROME_PORT=9333 后重试。
exit /b 1

:connector_ready

docker compose --env-file .env.docker up -d --build
if errorlevel 1 (
  echo [BossHunter] Docker 服务启动失败，请运行 docker-logs.cmd 查看日志。
  exit /b 1
)

echo [BossHunter] 正在等待服务健康检查...
for /l %%I in (1,1,60) do (
  curl.exe -fsS http://127.0.0.1:8686/api/health >nul 2>nul && goto ready
  timeout /t 2 /nobreak >nul
)
echo [BossHunter] 服务未在预期时间内就绪，请运行 docker-logs.cmd 查看日志。
exit /b 1

:ready
echo [BossHunter] 已启动：http://127.0.0.1:8686
start "" http://127.0.0.1:8686
exit /b 0
