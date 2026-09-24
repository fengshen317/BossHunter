# BossHunter GitHub 发布检查清单

本清单用于维护者在创建公开 GitHub 仓库、打标签或发布新版本前自检。

## 1. 仓库内容

- [ ] `git status` 中的所有文件都已逐项确认来源和用途。
- [ ] 未提交 `config.yaml`、`.env`、真实简历、头像、数据库或浏览器会话。
- [ ] 未提交 API Key、数据库密码、Bearer Token、内部域名或个人联系方式。
- [ ] `data/`、`node_modules/`、虚拟环境和临时输出均被忽略。
- [ ] 前端 `src/bosshunter/web/frontend/dist/` 与源码对应，且没有调试密钥。
- [ ] 演示图片、视频和日志已经脱敏，并确认拥有发布权。

建议执行：

```bash
git status --short
git diff --check
git ls-files config.yaml .env data
```

密钥扫描应使用团队认可的工具；一旦发现真实密钥，应先撤销并轮换，再清理 Git 历史，仅删除当前文件不够。

## 2. 项目信息

- [ ] README 中的版本、Python/Node.js/PostgreSQL 要求与 `pyproject.toml` 和源码一致。
- [ ] 仓库地址、Issue 地址和克隆命令指向实际 GitHub 仓库。
- [ ] README 不引用缺失的截图、GIF 或视频。
- [ ] `LICENSE` 与 `pyproject.toml` 均声明 BossHunter Non-Commercial License。
- [ ] README 明确说明项目与招聘平台无隶属、合作或背书关系。
- [ ] README 明确说明自动化风险、人工确认边界和非商业使用限制。

## 3. 文档

- [ ] [中文 README](../README.md) 与 [English README](../README_EN.md) 的关键能力和限制一致。
- [ ] [文档中心](README.md) 中的本地链接均有效。
- [ ] 安装指南包含 PostgreSQL 初始化和迁移顺序。
- [ ] 配置参考不包含真实凭据或内部服务信息。
- [ ] 故障排查说明不会鼓励绕过验证码、风控或人工确认。
- [ ] 贡献指南写明接受和拒绝的变更类型。

## 4. 验证

```bash
python -m pip install -e .
pytest -v
ruff check src/ tests/
bosshunter --help
bosshunter --version

cd src/bosshunter/web/frontend
npm ci
npm run build
```

若 PostgreSQL 集成测试需要额外测试数据库，应使用隔离的测试配置，不得指向真实生产或个人求职数据库。

## 5. GitHub 仓库设置

- [ ] 创建 Issue 模板，要求用户删除隐私信息和密钥。
- [ ] 启用 Secret scanning、Push protection 和 Dependabot（仓库权限允许时）。
- [ ] 设置默认分支保护，要求测试通过后合并。
- [ ] Topics、简介和网站地址准确，不使用招聘平台官方标识造成误解。
- [ ] Release Notes 列出功能变化、迁移步骤、风险和已知问题。
- [ ] 发布包不包含本地配置、数据库、简历或浏览器 Profile。

## 6. 发布后抽查

- [ ] 从全新目录克隆仓库，按安装指南完成一次干净安装。
- [ ] 检查 GitHub 页面上的中文文件名、表格、图片和相对链接。
- [ ] 验证示例配置可以通过应用校验。
- [ ] 用少量测试数据完成 `connect`、采集、评分和人工确认流程。
- [ ] 不执行真实批量发送；涉及真实平台的测试保持低频并由本人确认。

