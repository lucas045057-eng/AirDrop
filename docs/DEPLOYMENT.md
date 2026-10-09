# 部署与升级

Compose 项目名称沿用 `airdrop-v2-dev`，目录与资源保持隔离。不要全局清理 Docker、操作其他项目容器或覆盖已有密钥。

## 首次安装

常规安装使用 `bash scripts/start.sh`。约 2 GiB 内存的共享服务器使用 `bash scripts/start-server.sh`，串行构建限 384 MiB、0.5 CPU，并使用本项目资源限制。

`scripts/init_instance.py` 仅在缺少配置时生成随机管理员密码和主密钥，不创建业务数据。密码在 `secrets/dev-login.txt`，主密钥在 `secrets/master.key`；与 `.env` 一起私有保管。

API/前端默认绑定回环地址。固定 IP HTTPS 使用 `deploy/compose.public-ip.yml` 与 `scripts/enable-public-ip.sh`，不占用或修改其他服务的 80/443。自签证书不会自动获得浏览器信任。

## 管理模式与维护

保持 `.env` 中 `AIRDROP_EXECUTION_ENABLED=false`。调整并发或解除紧急停止不会打开执行。公网实例的 Compose 命令同时使用三个文件：

```bash
docker compose -p airdrop-v2-dev -f docker-compose.yml -f deploy/compose.small-server.yml -f deploy/compose.public-ip.yml ps
```

升级前在本项目私有数据卷中备份 SQLite，保留工件、主密钥与配置。源码不包含实际 `.env`、`secrets/`、`data/` 和 `server-runtime/`。backend 启动执行数据库迁移。

队列或管理模式代码变更须同时更新 backend/worker；界面变更更新 frontend。保持 Runner 无网络、无数据库/主密钥、只读根目录与资源限额。新镜像经隔离回归后更新服务，再核对登录、业务数据、主密钥及其他服务。

系统备份需私有保存，主密钥另行备份；源代码归档不能替代运行数据备份。
