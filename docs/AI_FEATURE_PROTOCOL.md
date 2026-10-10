# 功能协议与审核绑定

`packages/feature_protocol.py` 为严格 Pydantic 协议，未知字段拒绝。三个协议版本分别为 `airdrop-discovery-v1`、`airdrop-feature-v1`、`airdrop-feature-context-v1`。

## FeatureDefinition

每个功能包含稳定 feature_id、名称、描述、实际 entrypoint_candidate、source_functions、dependencies、shared_state、parameters、interaction_points、evidence、wallet_input、risk_level、confidence / confidence_kind、automation_status、blocking_reasons、生成 entrypoint、input_schema、execution_mode 与受限 permissions。

源引用格式：`relative/path.py:function`；依赖必须引用实际解析函数。Evidence 具有 file、line、end_line、type、function、脱敏 excerpt、confidence、confidence_kind、feasibility、manual_required，行号和内容须可回溯实际源码。规则与模型估计分别标注 `rule` / `model_estimate`。

交互分类为 MENU、PARAMETER、FILE_PATH、WALLET_ACCOUNT、CONFIRMATION、CAPTCHA_OTP、FUNDS_CONFIRMATION、UNKNOWN。确认、安全验证或资金操作不得默认批准、填充或移除。

参数类型为 string/integer/number/boolean/array/object/unknown，包含必填、明确默认值、敏感性、来源、有限约束和验证说明。未知类型须人工处理。生成 Schema 的 additionalProperties=false；敏感参数没有凭证默认值。任务中仅允许 `{ "reference": "credential_name" }` 引用，不能保存实际凭证。

钱包来源为 CSV/TXT/JSON/XLSX/environment/cli/custom/unknown；记录 source、fields、missing_fields、private_credentials_unavailable=true。字段角色为 identity、credential、account_config、project_config、feature_parameter。映射不是钱包数据；选择 credential 不授权读取私钥或签名。

## 模型产出

ModelPlan 严格接受 features、generated_files、replacements、unresolved。所选功能不能遗漏、替换为不存在函数或增加未知功能。生成文件必须一一对应所选入口；原函数及业务保留。高风险原调用链不能降低风险或生成自动入口。

输入最大 64,000 字节，相关源片段最大 24,000 字符；输出最大 512,000 字节；生成文件最多 20；替换文件最多 8。模型源码和指令不触发工具、网络链接、依赖安装或执行。

## 持久化与绑定

ScriptAnalysisJob：源版本/工件/明文 SHA、源 Manifest SHA 和流式计算的密文 SHA、幂等请求键、配置、阶段、结构结果、预览工件及审核 SHA、发送标记、取消、租约、次数、错误、管理员和时间。无完整源码重复存入数据库。

ScriptTransformationDraft：唯一任务、原始及产出工件/摘要、Diff 工件、配置摘要、审核 SHA、产出版本。review SHA 绑定源代码、产出代码与产出摘要（包含密文指纹），审批时检查当前源版本和工件指纹。

ScriptFeature：只能绑定草稿或产出版本，feature_key 唯一；definition、code SHA、approval SHA、status、approved_by/at。保存后的功能额外绑定版本 Manifest SHA 和产出密文 SHA。修改代码、配置、Manifest 或密文后不能创建功能任务。

Task：可空 script_feature_id 与 feature_config_snapshot。快照包含实际 entrypoint、参数 Schema/值或引用、钱包映射、code/approval SHA、execution_enabled=false。旧任务新增列为空，不转成猜测功能。

AI 发送必须满足当前源及密文指纹、完整脱敏上下文 SHA、Provider URL/模型/加密密钥指纹均未改变；实际发送使用保存的预览，不重新抓取实时数据库钱包。Provider 只允许公共 HTTPS，固定校验后的 IP，保留原 Host/TLS SNI，不跟随重定向，不信任环境代理。

## 管理 API

所有接口沿用管理员身份和修改请求 CSRF 校验。

| 方法与路径 | 功能 |
| --- | --- |
| POST /api/scripts/{version}/analysis-jobs | 创建 DISCOVER / TRANSFORM，返回 202 |
| GET /api/analysis/capabilities | Worker 心跳、队列/资源与现有 AI 配置状态 |
| GET /api/analysis-jobs?source_version_id=... | 最近 50 项任务 |
| GET /api/analysis-jobs/{job} | 实际阶段、错误、结果 |
| POST /api/analysis-jobs/{job}/cancel | 取消对应分析 |
| DELETE /api/analysis-jobs/{job} | 明确确认后清理终止记录 |
| GET /api/analysis-jobs/{job}/preview | 等待状态下不可变发送预览 |
| POST /api/analysis-jobs/{job}/approve-ai | confirmed=true 与精确 reviewed_preview_sha256 |
| GET /api/transformation-drafts/{draft} | 草稿摘要与功能 |
| GET /api/transformation-drafts/{draft}/diff | 文件过滤、offset、limit 1–400 |
| POST /api/script-features/{feature}/review | 每功能 APPROVED / REJECTED 与审核 SHA |
| POST /api/transformation-drafts/{draft}/save | 选择已批准 feature_ids 与草稿 SHA，新建版本 |
| GET /api/scripts/{version}/features | 该版本功能 |
| POST /api/tasks | script_feature_id、feature_parameters、wallet_input 管理配置 |

实际阶段：PENDING、SCANNING、DISCOVERING、WAITING_FOR_AI_APPROVAL、AI_ANALYZING、GENERATING、VALIDATING、REVIEW_REQUIRED、APPROVED、COMPLETED、FAILED、CANCELLED、MANUAL_REQUIRED。stage_progress.percent 始终为 null，网页不绘制虚构百分比。
