# V2.1 本地验收记录

日期：2026-10-10。状态：**COMPLETE（本地开发及管理范围）**。生产未部署；分支 `feature/v2.1-smart-adapter`，基线 `dc7e96c146cfade5458e521a38a9ac72eb003cee`。交付报告单独记录最终提交 HEAD。

环境为 Windows / PowerShell、本地 Python 3.13、Node 24、npm 11。测试使用新的 SQLite、临时数据目录、测试主密钥和测试账号，不连接生产服务器。浏览器使用本机已有 Chromium headless shell，通过 Playwright 驱动。没有付费或真实外部模型请求。

## 命令与实际结果

命令在项目目录执行；本次 Python 实际环境位于 `../../work/venv/Scripts/python.exe`。

| 检查 | 实际命令 | 结果 |
| --- | --- | --- |
| 修改前基线 | `python -m pytest -q` | 109 passed |
| 最终全量回归 | `python -m pytest -q --tb=short` | **218 passed，0 failed，129.87 秒** |
| 前端 | `npm run build`（frontend） | TypeScript 与 Vite 通过，1635 modules |
| 浏览器 | `node tests/browser/smart-analysis.mjs` | **22 项通过，0 页面运行错误** |
| 可选部署配置 | `docker compose --env-file .env.example -f docker-compose.yml -f deploy/compose.adapter.yml --profile smart-adapter config --quiet` | 退出 0，仅解析配置 |
| 文本完整性 | `git diff --check` | 无错误 |

全量测试包含一项原有 Starlette/httpx TestClient 弃用提示，不是功能失败。没有跳过失败测试或依赖付费 Provider。CI 配置已增加相同浏览器验收，但由于本次未推送，远程 CI 尚未运行。

Windows 浏览器实际调用额外设置了：`AIRDROP_QA_OUTPUT` 指向 work/smart-browser-complete，`AIRDROP_QA_CHROMIUM` 指向已有本地浏览器。Linux CI 使用安装的 Chromium，以及 `AIRDROP_QA_PYTHON=python`。测试夹具只在显式隔离标记下运行，自动创建独立数据，不提供生产 Fake 模型入口。

## 覆盖范围

静态：原生菜单、多文件、别名与多层输入包装、Click/Typer/Rich/questionary/inquirer/getpass/stdin/argparse、异步、多入口、无菜单模块公开根、动态及跨作用域冲突 UNKNOWN、同名 input 函数、默认表达式/装饰器/类/包初始化/间接导入副作用、CLI 库同名本地模块、共享状态、环境与配置/持久化、Windows 依赖、源码数量和语法预算。Click 入口调用实际 callback，CLI Context、复杂选项验证、父分组前置逻辑和运行时 argv 保持人工处理。

钱包：CSV/TXT/JSON/XLSX 来源引用、字段身份/凭证/账户/项目/功能角色、缺失映射、私钥与助记词默认值及代理凭证脱敏、非源码数据隔离。仅定义映射，没有读取真实钱包密钥。

AI/重构：错误结构 JSON、不存在函数或证据、非法路径、语法错误、错误权限、执行开关注入、Prompt Injection、业务丢失、恶意额外入口、凭证/交易模块改写、模型降级风险、参数默认值造假、无实际业务调用的占位入口、超时、取消、拒绝、Provider 变更、源/草稿/Manifest 变更、密文损坏以及有任务引用的删除保护。

队列：并发同键幂等、全局单活动任务、容量和取消释放、过期租约恢复一次、旧租约结果拒绝、发送后的模型请求不重放、整个处理时限取消悬挂模型、失败不产生部分版本/批准数据。

数据库：空数据库升级/降级/再次升级；真实历史结构的 receipts 兼容；V2 有旧数据升级后管理员登录、钱包数量、旧任务、批准版本与加密字节全部保留；Task 新字段为空，外键检查正常。

浏览器：上传交互源码 → 三个候选 → 明确 AI 发送审核 → 刷新恢复同一 SHA → Fake AI 入口草稿 → 实际分页 Diff → 逐功能批准 → 新版本 → 切换功能自动入口 → 明确默认值及数值参数 → 新建项目 → 功能任务快照 → 手机无横向溢出 → 保存后刷新 → 无效模型错误 → 活动模型取消 → OTP 人工阻塞 → 拒绝且禁止保存。三个已明确批准的 AI 任务总计三次 Fake 调用，没有自动重试。

## 内存和时间实测

以下为小型菜单包的 Windows 单次最终浏览器实测，不是生产容量保证。API 和 Adapter 主进程分开；分析/校验又在受限子进程中运行。测试 API/Worker 夹具额外加载测试模块，峰值不能等同于 Linux 镜像。

| 进程 | 测量方法 | 峰值 / 限制 |
| --- | --- | --- |
| 静态/校验子进程 | Windows GetProcessMemoryInfo PeakWorkingSet | 约 **39 MiB**；Job Object 256 MiB |
| Adapter 主进程 | 同上，独立进程 | **98.57 MiB** |
| API 主进程 | 同上，独立 Uvicorn | **109.89 MiB** |

受控大源码/32 MiB 子进程失败和取消测试中，健康检查均可响应；没有新版本或执行记录残留。原版本保存/失败回退在完整流程测试中验证。不会无限重试。单次小包扫描/校验约 0.3–0.4 秒，完整网页耗时包含人工审核操作和轮询，不作为性能基准。

默认队列 8、并发 1、分析子进程 256 MiB、上传子进程 96 MiB、任务总处理时限 90 秒、子进程最大 60 秒、模型请求 60 秒且最多一次。可选 Adapter 容器 512 MiB、0.5 CPU、PID 32。生产主机没有评估余量，未增加服务。

## 未执行 / 已知边界

- 本机 Docker daemon 未运行；Linux RLIMIT/cgroup 实际运行、容器重启集成、目标服务器余量及生产 Backend 热修复均 **未验证**。Compose 仅配置解析。
- PostgreSQL 迁移分支未实际运行；本地验收数据库为 SQLite。
- 未调用真实 Provider；实际供应商 JSON 质量、TLS 联通性及大项目稳定性未验证。SSRF/DNS 固定/重定向/响应预算使用 Mock 验证。
- 不执行上传代码，不安装依赖，功能 Runner 调用协议未开放；审核不是链上可运行证明。即使旧隔离引擎测试开关开启，多功能版本也禁止通过旧接口运行。
- 通用参数提取仅覆盖明确输入赋值；复杂状态、动态反射、类/嵌套绑定、验证码、认证、资金、安全检查和复杂交互进入人工处理。业务前置顺序不能凭名字猜测。
- 大包超出静态预算或子进程内存会可读失败/转人工，不能保证所有陌生项目都自动完成。
- 不能转为 Python 导入标识符的路径（如文件名含连字符）需要改名后重新上传；不会复制大量业务来绕过此限制。
- 工件写入后进程突然中断可能留下未登记加密文件；不会盲目在线删除，部署文档说明离线核对方法。

原有 `.env`、密钥、用户数据和生产实例均未修改；没有推送、合并或部署。
