# AirDrop V2

中文钱包、项目与 Python 脚本管理平台，使用 FastAPI、React/TypeScript、SQLAlchemy/Alembic 和 SQLite WAL，支持独立 Docker Compose 部署。

当前投产范围为**管理功能**：钱包生成与导入、分组、加密备份、浏览器内解密、脚本上传与分析、规则或外部 AI 适配、版本审核、项目绑定、任务资料和审计。**脚本执行、执行重试、自动调度、真实签名、RPC 与链上交易均关闭。**

仓库不提供演示初始化接口、模拟交易接口、演示样例或预置业务记录。首次安装只有管理员账号，钱包和项目由管理员创建。

## V2.1 智能脚本接入

新增多文件静态发现、交互式脚本功能拆分、SHA-256 绑定的脱敏 AI 发送审核、受控非交互入口、逐功能 Diff 审核、新版本和独立任务配置。原版本保留，真实执行和资金权限继续关闭。

脚本中心点击版本旁的 **智能分析** 开始。深度分析依赖可选的独立 Adapter Worker；默认 Compose 不会启动这个服务。

- [网页操作与分析边界](docs/AI_SCRIPT_ADAPTER.md)
- [功能协议与 API](docs/AI_FEATURE_PROTOCOL.md)
- [隔离部署、资源与回滚](docs/AI_ADAPTER_DEPLOYMENT.md)
- [实际验收结果](docs/AI_ADAPTER_VALIDATION.md)

本次仅完成本地开发与测试，生产部署需要另行授权，并核对现有 Backend 内存修复及服务器余量。Fake Provider 仅存在测试夹具中，不是生产配置选项。

## 启动

需要 Python 3.12+、Docker Engine 和 Docker Compose v2。仅在当前项目目录创建配置、密钥、专用容器和数据卷。

```bash
bash scripts/start.sh
```

Windows PowerShell：

```powershell
pwsh -File scripts/start.ps1
```

访问 <http://127.0.0.1:18080>。首次安装的随机管理员密码保存在 `secrets/dev-login.txt`，不得提交该文件。共享服务器启动与 HTTPS 配置见 [部署说明](docs/DEPLOYMENT.md)。

已有实例升级时保留 `.env`、`secrets/` 和数据卷，禁止重新生成主密钥。管理模式保持 `AIRDROP_EXECUTION_ENABLED=false`。

## 开发与检查

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
python scripts/init_instance.py
alembic upgrade head
pytest -q
```

```bash
cd frontend
npm ci
npm run build
```

队列与 Runner 回归只使用隔离数据库和开发者自己的受控测试输入，不会为部署开放执行或提供真实签名能力。

## 文档

- [操作流程](docs/OPERATION_GUIDE.md)
- [投产范围与限制](docs/PRODUCTION_STATUS.md)
- [部署和升级](docs/DEPLOYMENT.md)
- [密钥与备份](docs/SECURITY.md)
- [删除规则](docs/DELETION.md)

所有业务接口需要管理员会话，写操作需要 CSRF 校验。实际密码、私钥、主密钥、数据库、备份和依赖目录不进入 Git 或源码归档。
