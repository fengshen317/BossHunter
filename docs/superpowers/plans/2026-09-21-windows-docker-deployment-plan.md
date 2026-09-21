# Windows Docker 部署实施计划

## 目标

在不改变 BossHunter 业务状态机、人工确认和限流规则的前提下，增加仅支持 Windows + Docker Desktop 的部署方式。Docker运行 App与 PostgreSQL，Chrome/Edge及 Browser Connector运行在宿主机。

## 实施原则

- 测试先行：每项行为先补失败测试，再实现。
- 保持现有本地模式兼容；未配置 Token且监听回环地址时行为不变。
- 不提交真实 `.env.docker`、数据库、简历、浏览器 Profile或构建出的 Connector EXE。
- Docker默认仅映射 `127.0.0.1:8686`。
- PostgreSQL不映射宿主机端口。
- 每个阶段完成后运行聚焦测试；全部完成后再运行全量测试、Ruff和前端构建。

## 任务 1：补齐 Docker 配置覆盖能力

### 修改文件

- `src/bosshunter/config.py`
- `src/bosshunter/environment.py`
- `config.example.yaml`
- `tests/test_environment.py`
- `tests/test_db_routing.py`

### 测试先行

新增测试覆盖：

1. `BOSSHUNTER_DATABASE_URL` 覆盖 `config.yaml` 中的数据库 URL。
2. `BOSSHUNTER_BROWSER_PROXY_HOST`、`BOSSHUNTER_BROWSER_PROXY_PORT` 和 `BOSSHUNTER_BROWSER_PROXY_TOKEN` 注入浏览器配置。
3. `BOSSHUNTER_BASE_DIR` 决定配置与数据相对路径的解析基准。
4. 未设置环境变量时保持现有配置行为。
5. 配置诊断输出不得包含 Token和数据库密码。
6. 只有容器环境与 `BOSSHUNTER_TRUST_LOCAL_PORT_PUBLISH=true` 同时成立时，Web服务才允许在未配置管理 Token的情况下绑定 `0.0.0.0`；普通非回环绑定继续拒绝启动。

### 实现

- 在统一环境加载层读取 Docker环境变量，不在各模块重复读取。
- 新增浏览器 `proxy_token` 配置字段，但示例中只使用占位说明。
- 保持 `config.yaml` 兼容，并明确环境变量优先级高于文件配置。

### 验证

```bash
pytest tests/test_environment.py tests/test_db_routing.py -v
ruff check src/bosshunter/config.py src/bosshunter/environment.py tests/test_environment.py tests/test_db_routing.py
```

## 任务 2：为 Node Browser Runtime 增加安全监听与鉴权

### 修改文件

- `src/bosshunter/browser/runtime/cdp-proxy.mjs`
- `src/bosshunter/browser/runtime/__init__.py`
- `tests/test_browser_runtime_auth.py`
- `tests/test_browser_runtime.py`（如已存在则扩展）

### 测试先行

通过启动 Node子进程验证：

1. 默认监听 `127.0.0.1`，无 Token仍可使用，兼容本地模式。
2. `BOSSHUNTER_BROWSER_PROXY_HOST=0.0.0.0` 但没有 Token时进程拒绝启动。
3. 非回环模式下，无 Token或错误 Token请求返回 `401`。
4. 正确 Bearer Token可以访问 Runtime API。
5. `/health` 只返回 Runtime状态，不返回页面 URL、标题、Token或 CDP WebSocket地址。
6. Token不会出现在错误响应和进程日志中。

### 实现

- 支持 `BOSSHUNTER_BROWSER_PROXY_HOST`。
- 支持 `BOSSHUNTER_BROWSER_PROXY_TOKEN`。
- 对 Runtime端点统一增加 Bearer Token校验。
- 非回环监听时强制要求 Token。
- `_runtime_env()` 向本地 Node进程传递监听地址和 Token。
- 健康检查只暴露最小状态。

### 验证

```bash
pytest tests/test_browser_runtime_auth.py tests/test_browser_runtime.py -v
ruff check src/bosshunter/browser/runtime tests/test_browser_runtime_auth.py
```

## 任务 3：Python RuntimeClient 支持 Browser Token

### 修改文件

- `src/bosshunter/browser/client.py`
- `src/bosshunter/browser/runtime/__init__.py`
- `src/bosshunter/browser/__init__.py`
- `tests/test_browser_client.py`
- `tests/test_browser_facade.py`

### 测试先行

新增测试覆盖：

1. 配置 Token时，所有请求携带 `Authorization: Bearer <token>`。
2. 未配置 Token时不添加 Authorization头。
3. `401` 被识别为浏览器连接器鉴权失败，而不是普通业务空结果。
4. 异常和日志中不得包含原始 Token。
5. `runtime_health()`、`runtime_targets()` 和 RuntimeClient使用同一鉴权配置。

### 实现

- RuntimeClient构造函数接收 URL和可选 Token。
- Browser facade从进程级浏览器配置创建客户端，移除导入期固定 URL造成的配置漂移。
- 为鉴权失败增加明确但脱敏的诊断结果。

### 验证

```bash
pytest tests/test_browser_client.py tests/test_browser_facade.py -v
ruff check src/bosshunter/browser tests/test_browser_client.py tests/test_browser_facade.py
```

## 任务 4：实现 Windows Browser Connector CLI

### 新增或修改文件

- `src/bosshunter/browser/runtime/browser-connector.mjs`
- `src/bosshunter/browser/runtime/cdp-proxy.mjs`
- `src/bosshunter/browser/launcher.py`（仅提取可共享的浏览器查找约定时修改）
- `scripts/build_browser_connector.mjs`
- `scripts/sea-config.json`
- `tools/browser-connector/package.json`
- `tools/browser-connector/package-lock.json`
- `.gitignore`
- `tests/test_browser_connector_cli.py`

### 测试先行

使用临时目录和伪浏览器进程测试：

1. `init` 创建 `.env.docker` 并生成高熵数据库密码与 Browser Token。
2. 已存在 `.env.docker` 时不覆盖用户凭据。
3. `start` 能按 Chrome、Edge顺序查找浏览器，也支持显式路径。
4. 使用独立 Profile和 `127.0.0.1` CDP端口启动浏览器。
5. 端口占用时返回明确错误，不结束其他进程。
6. `health` 能区分浏览器未启动、CDP未就绪、Runtime未就绪和 Token错误。
7. `stop` 只停止 Connector，不删除 Profile。

### 实现

- CLI复用现有 Browser Runtime协议，不复制自动化业务逻辑。
- Connector启动 Chrome/Edge后在 `0.0.0.0:3456` 提供带 Token鉴权的 Runtime API。
- 使用 Node 22 Single Executable Application：先用 esbuild打包，再生成 SEA blob并注入 Windows `node.exe` 副本。
- 构建产物放入 `dist/connector/` 并加入 `.gitignore`；Release流程上传 EXE和 SHA256，不把 EXE提交到 Git。
- 源码模式允许使用 `node browser-connector.mjs` 完成同样操作。

### 验证

```bash
pytest tests/test_browser_connector_cli.py -v
npm run build:connector
dist\connector\bosshunter-browser-connector.exe health
```

## 任务 5：实现 App Docker 镜像与入口

### 新增或修改文件

- `Dockerfile`
- `.dockerignore`
- `docker/entrypoint.py`
- `docker/healthcheck.py`
- `pyproject.toml`
- `tests/test_docker_entrypoint.py`
- `tests/test_package_artifacts.py`

### 测试先行

新增测试覆盖：

1. PostgreSQL未就绪时按有限退避等待。
2. 超时后进程非零退出，不启动 Web服务。
3. 数据库迁移成功后才启动 App。
4. 迁移失败时保留原始错误并非零退出。
5. 配置或数据目录不可写时启动失败并给出明确路径。
6. Wheel包含前端 `dist`、Browser Runtime和迁移资源。

### 实现

- 多阶段镜像构建前端、Python wheel和最小运行镜像。
- 运行阶段使用非 root 用户。
- Entrypoint等待数据库、执行迁移并启动 `bosshunter-server`。
- 容器内设置外部 Browser Runtime配置，不尝试启动本地 Node代理。
- 仅在检测到容器环境时启用受限的本地端口发布模式；宿主机仍只映射 `127.0.0.1:8686`。
- 健康检查只验证 App和数据库基础状态，不执行招聘平台操作。

### 验证

```bash
pytest tests/test_docker_entrypoint.py tests/test_package_artifacts.py -v
docker build -t bosshunter:local .
```

## 任务 6：实现 Compose 与 Windows 启停脚本

### 新增文件

- `docker-compose.yml`
- `.env.docker.example`
- `docker-start.cmd`
- `docker-stop.cmd`
- `docker-status.cmd`
- `docker-logs.cmd`
- `tests/test_docker_distribution.py`

### 测试先行

新增静态和子进程测试覆盖：

1. Compose只包含 `app` 和 `postgres`。
2. App端口映射固定为 `127.0.0.1:8686:8686`。
3. PostgreSQL没有宿主机端口映射。
4. PostgreSQL和 App均有健康检查。
5. App通过 `host.docker.internal` 访问 Connector。
6. 数据库卷和 `docker-data` 挂载不会随普通停止删除。
7. 启动脚本按 Docker检查、Connector、Compose、健康检查顺序执行。
8. 脚本不会自动结束占用端口的其他进程。
9. 停止脚本默认不删除卷，也不关闭浏览器。

### 实现

- Compose使用固定 PostgreSQL major版本。
- 使用 `.env.docker` 注入随机密码和 Token。
- 配置日志轮转。
- `docker-start.cmd` 优先使用 Release中的 Connector EXE；缺失时显示下载完整 Release ZIP的提示。
- 健康检查失败时输出精确的日志查看命令。

### 验证

```bash
pytest tests/test_docker_distribution.py -v
docker compose --env-file .env.docker.example config
```

## 任务 7：完善忽略规则、示例配置与文档

### 修改文件

- `.gitignore`
- `README.md`
- `docs/操作手册.md`
- `docs/技术手册.md`
- `config.example.yaml`
- `.env.docker.example`

### 内容

- 明确项目使用 NonCommercial许可证，属于 source-available。
- 增加 Windows Docker快速开始。
- 解释 App、PostgreSQL和宿主机浏览器之间的关系。
- 写明首次登录、启停、日志、配置、备份和常见故障。
- 明确禁止把 8686、3456、9222暴露到公网。
- 提供 Token错误、Docker Desktop未启动、端口占用和浏览器未登录的排查步骤。
- 说明 `docker compose down` 与删除卷的差异。

### 验证

```bash
rg -n "your[_-]?(key|token|password)|example|placeholder" .env.docker.example config.example.yaml README.md
git diff --check
```

## 任务 8：整体回归与人工验收

### 自动验证

```bash
pytest -v
ruff check src/ tests/
npm ci
npm run build
docker build -t bosshunter:local .
docker compose --env-file .env.docker.example config
```

如果本机 `config.yaml` 指向外部 PostgreSQL导致测试污染，测试必须通过显式测试配置隔离，不能修改或删除用户配置。

### Windows人工验收

1. 在安装 Docker Desktop但未安装 Node.js的 Windows环境解压 Release ZIP。
2. 双击 `docker-start.cmd`。
3. 验证 Connector启动宿主机 Chrome/Edge和独立 Profile。
4. 在正常浏览器窗口登录招聘平台。
5. 打开控制台并验证 Browser Runtime连接。
6. 完成一次只读岗位采集和招呼语生成。
7. 验证发送动作仍要求人工确认。
8. 执行 `docker-stop.cmd` 后重新启动。
9. 验证数据库、配置、简历和浏览器登录状态保留。
10. 验证无 Token请求不能调用 Browser Runtime。

## 建议提交拆分

1. `feat: 增加浏览器运行时鉴权`
2. `feat: 添加Windows浏览器连接器`
3. `feat: 添加Docker应用镜像与启动入口`
4. `feat: 添加Windows Docker一键启停脚本`
5. `docs: 补充Windows Docker部署说明`

每个提交必须保持测试可运行，不把生成的 EXE、`.env.docker`、数据库或浏览器 Profile加入版本控制。
