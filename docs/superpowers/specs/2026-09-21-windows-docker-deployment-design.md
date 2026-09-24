# BossHunter Windows Docker 部署设计

## 1. 背景与目标

BossHunter 当前通过源码目录、Python 虚拟环境、Node.js Browser Runtime 和本地 PostgreSQL 运行。用户希望公开项目源码，同时降低部署门槛，并避免容器内浏览器带来的登录、验证码和使用体验问题。

首版 Docker 部署仅支持 Windows 10/11 + Docker Desktop。Docker 负责运行 BossHunter 应用和 PostgreSQL；Chrome/Edge 继续运行在 Windows 宿主机，通过带鉴权的 Browser Connector 与容器通信。

目标：

- 用户安装 Docker Desktop 后，可以通过 `docker-start.cmd` 启动完整服务。
- 用户在正常的宿主机 Chrome/Edge 窗口中扫码或手动登录招聘平台。
- 浏览器登录状态、配置、简历、生成文件和数据库在容器重建后仍然保留。
- Web 控制台、数据库和浏览器控制接口默认不向公网暴露。
- 不改变人工确认、发送时间窗口、频率限制和风险控制规则。

非目标：

- 容器内 Chromium、noVNC 或远程桌面。
- macOS、Linux 桌面或 Linux 服务器部署。
- Windows 非 Docker 一键安装器。
- Redis 多实例 Worker、自动更新或自动回滚。
- 公网部署和多用户 SaaS。

## 2. 用户体验

仓库提供以下入口：

```text
docker-start.cmd       启动浏览器连接器和 Compose 服务
docker-stop.cmd        停止 Compose 服务，默认保留浏览器
docker-status.cmd      显示 App、数据库和连接器状态
docker-logs.cmd        查看应用日志
.env.docker.example    Docker 环境变量模板
docker-compose.yml     App 与 PostgreSQL 编排
```

首次启动流程：

1. 用户安装并启动 Docker Desktop。
2. 用户双击 `docker-start.cmd`。
3. 脚本检查 Docker Desktop 和 Compose 是否可用。
4. Browser Connector 创建 `.env.docker`，生成数据库密码和 Browser Runtime Token。
5. Browser Connector查找并启动宿主机 Chrome/Edge，使用项目专用 Profile。
6. Compose构建或拉取镜像，启动 PostgreSQL和 BossHunter App。
7. App等待数据库健康，执行迁移，然后启动 Web服务。
8. 脚本轮询 App和 Browser Runtime健康状态。
9. 默认浏览器打开 `http://127.0.0.1:8686`；用户在自动化浏览器窗口中登录招聘平台。

后续启动复用 `.env.docker`、数据库卷和浏览器 Profile，不重新生成凭据，也不清除登录状态。

## 3. 总体架构

```text
Windows 宿主机
├── Chrome / Edge
│   └── CDP 127.0.0.1:9222
├── bosshunter-browser-connector.exe
│   ├── 连接本机 CDP
│   └── Runtime API 0.0.0.0:3456 + Token
└── Docker Desktop
    └── Compose 网络
        ├── bosshunter-app
        │   ├── Web/API 0.0.0.0:8686
        │   └── 访问 host.docker.internal:3456
        └── bosshunter-postgres
            └── 仅 Compose 内部端口 5432
```

宿主机映射仅包含：

```yaml
ports:
  - "127.0.0.1:8686:8686"
```

PostgreSQL不映射到宿主机。Browser Connector必须校验 Token，Chrome原始 CDP端口始终只监听回环地址。

## 4. App 镜像

使用多阶段 Dockerfile：

1. Node构建阶段运行 `npm ci` 和 `npm run build`。
2. Python构建阶段构建 BossHunter wheel。
3. Python 3.11 slim运行阶段只复制 wheel、前端产物、迁移和入口脚本。
4. 创建非 root 用户运行应用。
5. 暴露 8686，仅由 Compose映射到宿主机回环地址。

容器中的 Web服务必须绑定 `0.0.0.0` 才能接受 Docker端口转发。新增显式的容器本地发布模式：只有同时检测到容器环境、设置 `BOSSHUNTER_TRUST_LOCAL_PORT_PUBLISH=true`，且正式 Compose仍将端口发布到宿主机 `127.0.0.1` 时，才允许不配置 Web管理 Token。普通非回环部署继续执行现有的强制鉴权策略。

运行镜像不需要启动 Chromium，也不需要自动启动内置 Browser Runtime。容器配置将浏览器 Runtime设为外部服务：

```yaml
browser:
  runtime: external
  proxy_host: host.docker.internal
  proxy_port: 3456
  auto_start_proxy: false
```

应用入口：

1. 校验必要环境变量。
2. 等待 PostgreSQL健康。
3. 执行幂等数据库迁移。
4. 校验配置文件和数据目录权限。
5. 启动 `bosshunter-server`，绑定 `0.0.0.0:8686`。

## 5. PostgreSQL 与持久化

Compose使用官方 PostgreSQL镜像，并配置健康检查。

持久化内容：

```text
bosshunter-postgres-data    PostgreSQL 数据
./docker-data/config        config.yaml
./docker-data/data          简历、生成文件、日志和应用数据
./docker-data/browser       宿主机浏览器 Profile
```

`.env.docker` 保存随机数据库密码、Browser Runtime Token和可选 AI凭据，必须加入 `.gitignore`。`docker compose down` 不删除数据卷；只有用户明确执行带 `--volumes` 的清理命令才删除数据库。

数据库迁移必须满足：

- 容器重复启动时可安全重复检查。
- 迁移失败时 App不得开始监听。
- 日志明确显示失败的迁移编号。
- 不在启动脚本中自动删除或重建现有数据库。

## 6. Windows Browser Connector

复用当前 `cdp-proxy.mjs` 的协议和端点，并增加：

- 可配置监听地址 `BOSSHUNTER_BROWSER_PROXY_HOST`。
- 必填 Token `BOSSHUNTER_BROWSER_PROXY_TOKEN`。
- 对除健康探针外的 Runtime请求校验 `Authorization: Bearer <token>`。
- 健康响应不得返回页面 URL、标题或登录信息。
- 可配置 Chrome主机；宿主机模式保持 `127.0.0.1`。
- 优雅退出和明确日志文件。

Connector使用 Node.js Single Executable Application 构建为独立 Windows可执行文件。最终发布 ZIP包含该文件，用户无需安装 Node.js。源码开发模式仍可直接运行 `.mjs`。

Connector命令：

```text
bosshunter-browser-connector.exe init
bosshunter-browser-connector.exe start
bosshunter-browser-connector.exe health
bosshunter-browser-connector.exe stop
```

`init` 负责创建 `.env.docker`、生成随机 Token并准备浏览器 Profile目录。`start` 负责查找 Chrome/Edge、启动远程调试实例并启动 Runtime API。

## 7. App 到 Connector 的鉴权

Python `RuntimeClient` 从配置或环境变量读取 Token，在每个 Runtime请求中加入 Bearer Token。Token不得写入日志、异常正文或前端响应。

为了兼容现有本地模式：

- 未配置 Token且 Runtime只监听 `127.0.0.1` 时，保持当前无 Token行为。
- Runtime监听非回环地址时，没有 Token必须拒绝启动。
- Docker模式始终配置 Token。

Connector虽然监听宿主机接口，但只有持有随机 Token的容器可以调用。安装说明仍应提醒用户不要在路由器或云安全组中开放 3456端口。

## 8. Compose 配置

Compose只包含两个容器服务：

### `app`

- 依赖健康的 `postgres`。
- 从 `.env.docker` 读取数据库 URL和 Browser Runtime Token。
- 设置受限的本地容器发布标志，使 Web服务可以通过宿主机回环端口访问；该标志不得用于公网 Compose配置。
- 通过 `extra_hosts: host.docker.internal:host-gateway` 保留明确映射。
- 挂载只读配置和可写数据目录。
- 健康检查访问 `/api/health`。
- 设置日志轮转上限。

### `postgres`

- 使用固定 major版本镜像，避免无意跨版本升级。
- 使用命名卷保存数据。
- 配置 `pg_isready` 健康检查。
- 不声明宿主机端口映射。

Redis不进入首版，因为默认单实例任务执行不依赖 Redis。未来启用多实例 Worker时再通过 Compose profile增加。

## 9. 启停和错误处理

`docker-start.cmd` 在任一检查失败时停止后续步骤，并显示可操作的信息：

- Docker Desktop未启动：提示启动后重试。
- Connector缺失：提示下载完整 Release ZIP，而不是静默尝试 Node。
- Chrome/Edge未安装：提示支持的浏览器和配置方式。
- 3456、9222或8686被占用：显示占用端口，不自动终止其他进程。
- PostgreSQL未健康：输出 `docker compose logs postgres` 的查看命令。
- App迁移失败：保持数据库和日志，不重建数据卷。
- Connector不可达或 Token错误：Web界面可打开，但浏览器操作显示明确诊断，不得伪装为岗位不存在。

`docker-stop.cmd` 默认执行 `docker compose stop app postgres`，不删除卷，并允许 Browser Connector继续运行。脚本提供显式参数关闭 Connector。

## 10. 安全边界

- Web控制台默认仅监听宿主机 `127.0.0.1`。
- PostgreSQL只存在于 Compose网络。
- Chrome原始 CDP只监听宿主机回环地址。
- 非回环 Browser Runtime必须启用高熵 Token。
- 所有示例密钥均使用明显占位值。
- `.env.docker`、`docker-data/`、简历、数据库和浏览器 Profile均被 Git忽略。
- 镜像中不得包含 `config.yaml`、`.env`、测试数据库或本地浏览器数据。
- 人工确认、限流、发送窗口和已有安全检查不得因 Docker模式而放宽。

## 11. 测试与验收

自动测试：

- Docker镜像可以完成构建。
- Compose配置可通过 `docker compose config`。
- App在 PostgreSQL未就绪时等待，在迁移失败时退出。
- RuntimeClient正确添加 Token，且不会在异常中泄露 Token。
- Connector非回环监听但无 Token时拒绝启动。
- 无 Token和错误 Token请求被拒绝。
- 本地回环无 Token模式保持兼容。
- 配置、简历和生成文件路径在容器内正确解析。

Windows人工验收：

1. 全新 Docker Desktop环境执行 `docker-start.cmd`。
2. Chrome/Edge正常打开并显示招聘平台登录页。
3. 登录后刷新容器不丢失浏览器 Profile。
4. Web控制台可以检测浏览器连接。
5. 完成一次岗位采集和招呼语生成。
6. 发送前仍要求人工确认。
7. `docker compose down` 后重新启动，数据库和配置仍存在。
8. 其他局域网设备无法访问 8686；无 Token无法调用 3456。

首版完成标准：

- Windows用户仅需预装 Docker Desktop和 Chrome/Edge。
- Release ZIP内双击 `docker-start.cmd` 可以启动服务。
- 浏览器登录发生在宿主机正常窗口中。
- Docker容器不包含浏览器和用户登录态。
- 容器重建不丢失数据库、配置、简历和浏览器 Profile。
- README包含安装、启动、停止、日志、备份和常见故障说明。

## 12. 实施边界

实现阶段应尽量复用现有 Browser Runtime和配置结构，不重写浏览器自动化协议。新增鉴权必须同时覆盖 Python客户端、Node Runtime及其测试。Docker入口只负责部署，不改变业务状态机或发送安全规则。
