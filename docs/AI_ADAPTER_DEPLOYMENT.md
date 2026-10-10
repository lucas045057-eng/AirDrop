# 智能分析的隔离部署与回滚

## 当前交付范围

V2.1 已在本地隔离数据库、临时数据目录和专用分支开发。没有 SSH 登录生产、修改线上 Compose、重建容器、新增常驻服务、读取钱包凭证或推送/合并仓库。线上临时调整过的 Backend 内存上限未检查，也未覆盖。

服务器约 1.7 GiB 并有其他业务。部署前必须取得授权，并只读核对当前配置和真实余量。仓库 `deploy/compose.small-server.yml` 的旧 Backend 192 MiB 设置不能作为线上实际状态，也不能自动应用覆盖修复。

## 本地启动

使用项目既有 Python 环境与依赖；复制 `.env.example` 到专用实例目录的 `.env`，通过已有 `scripts/init_instance.py` 初始化测试密钥与管理员。确认 data_dir、database_url、master_key_file 指向该实例，不使用生产文件。管理开关为 false。

在项目目录，使用同一组实例环境变量分别运行：

```text
python -m alembic upgrade head
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
python -m worker.adapter_worker
cd frontend
npm ci
npm run dev
```

前端开发代理默认访问 127.0.0.1:8000，可通过 `AIRDROP_DEV_API_TARGET` 指向另一个本地测试端口；生产 Nginx 代理配置没有改变。allowed_origins 应包含当前开发网页来源。系统设置的 Provider 沿用原有 HTTPS URL、模型和加密密钥。仅在明确审核后才发送真实模型请求。

`tests/browser/serve.py` 只能在 `AIRDROP_BROWSER_QA=local-test-only` 的隔离环境启动，是测试夹具，不是生产 Provider 或设置选项。浏览器脚本自己创建新的数据库、密钥、随机本地端口和三个进程，最后仅终止自己启动的进程。不要把测试实例指向真实 data_dir。

## 可选容器服务

`deploy/compose.adapter.yml` 提供 **smart-adapter** profile，默认不会启动。Compose 配置解析已在本地验证；本机没有运行的 Docker daemon，因此 Linux 容器运行、cgroup 内存/CPU 和目标主机余量均未验证。

在获得生产部署授权、备份完成且资源评估通过后，可在现有独立项目目录采用以下结构。命令为将来部署参考，本次没有执行启动：

```text
docker compose -f docker-compose.yml -f deploy/compose.adapter.yml --profile smart-adapter up -d adapter
```

需要合并的线上 override 以现场核对结果为准，不能擅自附加旧 small-server 配置。服务挂载已有 app-data 与只读 service-secrets，不挂载 runner-bus。只读根目录、cap_drop ALL、no-new-privileges、PID 32、临时目录 16 MiB；网络仅为审核过的模型请求，不影响隔离 Runner 的无网络配置。

| 限制 | 默认 |
| --- | --- |
| Adapter 并发 | 全局 1，由数据库锁与租约协调 |
| 持久化排队/活动任务 | 8（可配 1–32，等待发送审核也占位） |
| Adapter 容器内存 / CPU | 512 MiB / 0.5 CPU，包含主进程与子进程 |
| 单个分析子进程 | 256 MiB（可配 32–512） |
| 上传解析子进程 | 96 MiB，单 API 进程并发 1 |
| 单个子进程时限 | 最大 60 秒，上传 30 秒 |
| 整个分析任务时限 | 90 秒（可配 5–180） |
| Linux 子进程 CPU 时间 | 60 秒；AS、文件尺寸、文件描述符和 core 另设限制 |
| Windows 子进程 | Job Object 进程/总内存限制，取消/时限终止自己进程树 |
| 模型调用 | 每任务最多 1 次；不自动重试 |

容器内存不能直接设为与子进程同值：还须给 Python 主进程、加密解码和数据库分配余量。本地峰值见验收文档，不能当作生产大包的容量保证。余量不足时保持 profile 关闭，拒绝提交或取消积压任务；本地继续分析。不得让本次工作影响服务器其他业务。

## 迁移前备份

1. 在获得维护授权后停止该项目的写入进程。不要停止服务器其他服务。
2. 用 SQLite backup API 或在所有本项目写入停止后复制完整 DB，并保留 WAL/SHM 一致性；同时备份 data/artifacts 与原 master.key。备份须有受限权限，密钥单独保管。
3. 记录当前镜像、Git HEAD、Compose 和现场资源 override。校验备份可读取、密钥可解密工件，不把密钥或数据库提交 Git。
4. 运行 Alembic 到 `c91e2a6d7101`，核对旧管理员登录、钱包数量、任务、版本与原加密文件未改变，再启用经批准的服务。保留执行开关 false。

迁移新增三个表及 Task 的两个可空列，不修改旧数据。SQLite 使用原生 nullable ADD COLUMN，避免重建有执行历史引用的 tasks 表；外键始终启用。PostgreSQL 分支已编写但本地未实际运行。

## 故障恢复与回滚

Worker 租约过期时，静态任务仅恢复一次；再次中断或请求可能已发送时转 MANUAL_REQUIRED。已有草稿/保存版本不覆盖，调用次数不自动重置。取消等待审核的任务即可释放队列容量。失败后选择功能新建任务，AI 必须重新审核。

若只关闭智能分析服务，原有 V2 管理功能继续工作；等待队列保留。若需回退代码，先停本项目写入，按同一时点恢复数据库、加密工件和密钥以及原镜像。不要混用新数据库和旧代码。

```text
python -m alembic downgrade 71a960e315dc
```

此命令仅适用于备份完成、已明确处理新增分析数据且没有功能任务的测试/维护实例。仍有 script_feature_id 任务时迁移主动拒绝，避免丢失配置。downgrade 会删除新增分析表，所以生产首选整套备份恢复。空数据库 up/down/up 和有旧数据的升级已测试；生产未执行。

进程在产出加密文件后、提交工件行前突然终止，可能留有未登记的小型加密文件。本版不会在运行时盲删未知文件。若需要磁盘清理，停本项目写入并备份后按数据库全部引用进行离线核对；不要删除任何仍被版本或草稿引用的文件。
