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

## 原生脚本运行（独立 opt-in）

原生容器运行与旧模拟 Runner 使用不同开关。新实例和升级实例都应保持 `AIRDROP_NATIVE_EXECUTION_ENABLED=false`，直到完成目标 Linux 主机的隔离验收。`AIRDROP_EXECUTION_ENABLED` 继续控制旧模拟执行和计划任务；本功能不开放自动调度。

### 上线前检查

1. 仅在专用 Linux 主机上部署。确认 Docker daemon、SQLite 数据目录、工件目录、主密钥备份、磁盘余量、`/dev/shm` 余量及其他服务的资源边界。Windows 假 Docker 浏览器测试不等价于 Linux 容器隔离验收。
2. 在 `.env` 明确配置原生运行参数，先保持总开关关闭：

   ```dotenv
   AIRDROP_NATIVE_EXECUTION_ENABLED=false
   AIRDROP_NATIVE_DOCKER_HOST=unix:///var/run/docker.sock
   AIRDROP_NATIVE_NETWORK=airdrop-native
   AIRDROP_NATIVE_NETWORK_SUBNET=172.31.240.0/24
   AIRDROP_NATIVE_SOCKET_PATH=/sockets/native.sock
   AIRDROP_NATIVE_SOCKET_GID=10001
   AIRDROP_NATIVE_RUN_ROOT=/dev/shm/airdrop-runs
   AIRDROP_NATIVE_PYPI_INDEX_URL=https://pypi.org/simple
   AIRDROP_DOCKER_CLI_IMAGE=docker:27.5.1-cli
   AIRDROP_NATIVE_RUNTIME_BASE_IMAGE=registry.example/airdrop-python@sha256:<已核实摘要>
   ```

   将示例镜像改为已审核、不可变摘要的内部镜像；确认子网与宿主、Docker 和其他容器网络不重叠。`deploy/native-runtime.Dockerfile` 可作为 Python 3.12 基础镜像来源，生产配置仍应固定到镜像摘要。
3. 先创建专用网络，再应用宿主防火墙。子网必须与配置相同：

   ```bash
   docker network create --driver bridge --subnet 172.31.240.0/24 \
     --opt com.docker.network.bridge.enable_icc=false \
     --label airdrop.managed=1 airdrop-native
   sudo env AIRDROP_NATIVE_NETWORK=airdrop-native \
     AIRDROP_NATIVE_BLOCKED_TARGETS="169.254.169.254 169.254.170.2 100.100.100.200" \
     sh deploy/native-runtime-firewall.sh
   ```

   `DOCKER-USER` 规则拦截配置的元数据地址与敏感转发网段；`INPUT` 规则阻断专用网桥到宿主所有本机地址和端口。脚本默认不会开放宿主管理服务给业务容器。若主机使用 nftables 或其他防火墙，先建立等价规则。控制器启动前会用临时受控监听器验证宿主 `INPUT`，并逐个检查配置的转发目标；监听器未能从宿主侧访问、目标仍可达、连接被拒绝或探针状态不明时，均拒绝启动业务容器。
4. 确认 `/dev/shm/airdrop-runs` 是本次部署的专用目录、与容器内路径相同，并有足够容量。该目录承载短期运行输入和状态交换，不用于备份；运行结束后按生命周期清理。
5. 按现有发布流程构建 Backend 镜像，并确认 `.env`、`service-secrets`、`app-data` 均属于本 Compose 项目。依赖构建只接受标准包要求，从配置的 HTTPS 包索引下载 wheels，再在离线阶段安装；没有可用 wheel 的依赖会明确失败，需要人工准备可用版本。先执行迁移和管理功能回归，备份数据库、所有 `data/artifacts` 与配置；主密钥单独保管。

### 启动与受控验收

使用与当前实例完全相同的基础 Compose 文件，并追加 `deploy/compose.adapter.yml` 与 `deploy/compose.native.yml`。小服务器示例：

```bash
docker compose -p airdrop-v2-dev \
  -f docker-compose.yml \
  -f deploy/compose.small-server.yml \
  -f deploy/compose.adapter.yml \
  -f deploy/compose.native.yml \
  --profile smart-adapter config
docker compose -p airdrop-v2-dev \
  -f docker-compose.yml \
  -f deploy/compose.small-server.yml \
  -f deploy/compose.adapter.yml \
  -f deploy/compose.native.yml \
  --profile smart-adapter up -d controller backend worker adapter
```

先核对合并后的配置。只有独立 `controller` 镜像装有 Docker CLI/Buildx 并挂载 Docker socket；它以 root 身份运行，仅额外保留 `CHOWN` capability，用于设置容器运行目录和共享控制 socket 的组。Backend 与 Worker 共享组 ID `10001` 的本地控制 socket；Adapter 不挂载控制 socket。Backend、Worker、Adapter 都不能挂载 Docker socket。不要将控制器端口发布到宿主或公网。控制器容器的只读根目录、资源限额和专用运行目录必须保持启用。

验证顺序：迁移到最新版本；确认 Backend、控制器健康、Worker 与 Adapter 在线；在 Web 中审核运行定义并准备依赖环境；使用无真实凭据的受控样本运行，核对网络隔离探针、日志完整下载、状态回写和退出结果；检查备份可恢复后，再允许管理员通过该 opt-in Compose 覆盖启动原生服务。变更后重新检查系统能力页、执行记录和宿主防火墙规则。不要仅为打开原生能力而设置 `AIRDROP_EXECUTION_ENABLED=true`。

### 升级、备份与回退

升级前用 SQLite Backup API 创建一致性快照，并与数据库一并备份全部 `data/artifacts`、`.env`、Compose 覆盖文件及主密钥；日志分段、账号导入草稿、运行输入和状态快照都引用加密工件。`scripts/backup_system.py <备份文件>` 以 1 MiB 的独立 Fernet 分段流式加密，不把整个归档装入内存；归档密钥与主密钥必须分别私有保存。恢复到新目录可运行 `scripts/backup_system.py --restore-archive <备份文件> --key <备份密钥> --restore-to <新目录>`，程序会验证 SQLite 完整性、工件引用并重定位工件路径。容器镜像缓存不是备份；恢复后若镜像 ID 不存在，应在重新开放运行前重新准备并核对环境。

遇到隔离、日志或状态异常时，先关闭 `AIRDROP_NATIVE_EXECUTION_ENABLED` 并重建 Backend/Worker，核查正在运行的容器与钱包锁；结果未知时先完成执行中心人工核查。回退代码前恢复同一时间点的数据库、工件、配置和主密钥，不要让旧代码读取新表结构。迁移 downgrade 会删除本阶段新增的运行定义、账号资料、状态及日志表；生产回退优先恢复完整备份。升级前后都不要全局清理 Docker 或修改其他 Compose 项目。
