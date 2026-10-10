import { useEffect, useState } from "react";
import { ArrowLeft, Check, RefreshCw, Sparkles } from "lucide-react";
import "./smart-analysis.css";

type Row = Record<string, any>;
type Props = {
  version: Row;
  api: (path: string, method?: string, body?: any) => Promise<any>;
  projects: Row[];
  refresh: () => Promise<any>;
  back: () => void;
  settings: () => void;
};
const names: Row = {
  PENDING: "等待分析",
  SCANNING: "扫描源码",
  DISCOVERING: "发现功能",
  WAITING_FOR_AI_APPROVAL: "等待审核发送内容",
  AI_ANALYZING: "模型规划中",
  GENERATING: "生成入口",
  VALIDATING: "校验重构",
  REVIEW_REQUIRED: "等待人工审核",
  APPROVED: "已有功能通过审核",
  COMPLETED: "已保存新版本",
  FAILED: "分析失败",
  CANCELLED: "已取消",
  MANUAL_REQUIRED: "需要人工处理",
  UNKNOWN: "结构未确定",
  REJECTED: "已拒绝",
};
const live = new Set([
  "PENDING",
  "SCANNING",
  "DISCOVERING",
  "AI_ANALYZING",
  "GENERATING",
  "VALIDATING",
]);
const pretty = (v: any) => JSON.stringify(v, null, 2);
const requestKey = () => crypto.randomUUID().replaceAll("-", "");
function initialJob() {
  return new URLSearchParams(location.hash.slice(1)).get("job") || "";
}
function section(title: string, children: React.ReactNode) {
  return (
    <section className="panel smart-section">
      <div className="panel-title">
        <h2>{title}</h2>
      </div>
      {children}
    </section>
  );
}

function Feature({
  feature,
  selected,
  onSelect,
  children,
}: {
  feature: Row;
  selected?: boolean;
  onSelect?: () => void;
  children?: React.ReactNode;
}) {
  const f = feature.definition || feature;
  return (
    <article className="smart-feature" data-feature={f.feature_id}>
      <div className="smart-feature-title">
        {onSelect && (
          <input
            type="checkbox"
            aria-label={`选择功能 ${f.name}`}
            checked={selected}
            onChange={onSelect}
          />
        )}
        <strong>{f.name}</strong>
        <span className="smart-status">
          {names[feature.status || f.automation_status]}
        </span>
      </div>
      <p>{f.description}</p>
      <dl className="smart-facts">
        <div>
          <dt>实际来源</dt>
          <dd>{f.source_functions.join("、")}</dd>
        </div>
        <div>
          <dt>调用依赖</dt>
          <dd>{f.dependencies.join("、") || "未发现静态调用依赖"}</dd>
        </div>
        <div>
          <dt>共享状态</dt>
          <dd>{f.shared_state.join("、") || "未发现共享全局状态"}</dd>
        </div>
        <div>
          <dt>风险 / 置信度</dt>
          <dd>
            {
              ({ low: "低", medium: "中", high: "高", unknown: "未知" } as Row)[
                f.risk_level
              ]
            }{" "}
            · {f.confidence_kind === "rule" ? "规则置信度" : "模型估计"}{" "}
            {f.confidence}（非成功概率）
          </dd>
        </div>
        <div>
          <dt>调用入口</dt>
          <dd>{f.entrypoint || "待生成或人工处理"}</dd>
        </div>
      </dl>
      {!!f.blocking_reasons.length && (
        <div className="smart-warning">
          <strong>阻塞原因</strong>
          <ul>
            {f.blocking_reasons.map((reason: string) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <p>
            请修改原项目的交互或安全结构后重新上传；平台不会自动确认验证码或资金操作。
          </p>
        </div>
      )}
      <details>
        <summary>参数、钱包映射与源码证据</summary>
        <div className="smart-table">
          <table>
            <thead>
              <tr>
                <th>参数</th>
                <th>类型 / 来源</th>
                <th>必填 / 默认值</th>
              </tr>
            </thead>
            <tbody>
              {f.parameters.map((p: Row) => (
                <tr key={p.name}>
                  <td>
                    {p.name}
                    {p.sensitive && "（凭证引用）"}
                  </td>
                  <td>
                    {p.type} / {p.source}
                  </td>
                  <td>
                    {p.required
                      ? "必填"
                      : p.has_default
                        ? pretty(p.default)
                        : "可省略"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          钱包来源：{f.wallet_input.mode} · {f.wallet_input.source || "未确定"}
          。私钥和助记词不会注入适配流程。
        </p>
        <div className="smart-tags">
          {Object.entries(f.wallet_input.fields).map(([key, role]) => (
            <span key={key}>
              {key}：{String(role)}
            </span>
          ))}
        </div>
        {f.wallet_input.missing_fields.length > 0 && (
          <p>缺少映射：{f.wallet_input.missing_fields.join("、")}</p>
        )}
        {f.evidence.map((e: Row, i: number) => (
          <div className="smart-evidence" key={i}>
            <small>
              {e.file}:{e.line}–{e.end_line} · {e.type}
            </small>
            <pre>{e.excerpt}</pre>
          </div>
        ))}
      </details>
      {children}
    </article>
  );
}

export default function SmartAnalysis({
  version,
  api,
  projects,
  refresh,
  back,
  settings,
}: Props) {
  const [jobId, setJobId] = useState(initialJob),
    [job, setJob] = useState<Row | null>(null);
  const [history, setHistory] = useState<Row[]>([]),
    [cap, setCap] = useState<Row>({});
  const [selected, setSelected] = useState<string[]>([]),
    [useAI, setUseAI] = useState(false);
  const [draft, setDraft] = useState<Row | null>(null),
    [preview, setPreview] = useState<Row | null>(null);
  const [checkedAI, setCheckedAI] = useState(false),
    [checkedDiff, setCheckedDiff] = useState(false);
  const [diff, setDiff] = useState<Row | null>(null),
    [diffFile, setDiffFile] = useState(""),
    [diffOffset, setDiffOffset] = useState(0);
  const [features, setFeatures] = useState<Row[]>([]),
    [savedVersion, setSavedVersion] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [taskFeature, setTaskFeature] = useState(""),
    [taskName, setTaskName] = useState("");
  const [projectId, setProjectId] = useState(""),
    [newProject, setNewProject] = useState("");
  const [parameters, setParameters] = useState<Row>({}),
    [wallet, setWallet] = useState<Row | null>(null);
  const [confirmedCleanup, setConfirmedCleanup] = useState(false);
  const [batchSize, setBatchSize] = useState(100),
    [retries, setRetries] = useState(0);
  const [pollEpoch, setPollEpoch] = useState(0);

  async function act(action: () => Promise<any>, message = "") {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      if (message) setNotice(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function reloadHistory() {
    const [rows, capabilities] = await Promise.all([
      api(`/analysis-jobs?source_version_id=${version.id}`),
      api("/analysis/capabilities"),
    ]);
    setHistory(rows);
    setCap(capabilities);
  }
  function chooseJob(id: string) {
    setJobId(id);
    setJob(null);
    setDraft(null);
    setPreview(null);
    setDiff(null);
    setDiffOffset(0);
    setDiffFile("");
    setCheckedAI(false);
    setCheckedDiff(false);
    setSelected([]);
    setConfirmedCleanup(false);
    location.hash = new URLSearchParams({
      analysis: version.id,
      ...(id ? { job: id } : {}),
    }).toString();
  }
  useEffect(() => {
    act(reloadHistory);
  }, [version.id]);
  useEffect(() => {
    let alive = true,
      timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!jobId) return;
      try {
        const row = await api(`/analysis-jobs/${jobId}`);
        if (!alive) return;
        if (row.source_version_id !== version.id)
          throw new Error("当前任务不属于此源码版本");
        setJob(row);
        if (row.status === "WAITING_FOR_AI_APPROVAL") {
          const p = await api(`/analysis-jobs/${jobId}/preview`);
          if (alive) setPreview(p);
        }
        if (row.result?.draft_id) {
          const d = await api(`/transformation-drafts/${row.result.draft_id}`);
          if (alive) setDraft(d);
          if (d.result_version_id) {
            const fs = await api(`/scripts/${d.result_version_id}/features`);
            if (alive) {
              setFeatures(fs);
              setSavedVersion(d.result_version_id);
            }
          }
        }
        if (live.has(row.status)) timer = setTimeout(poll, 1500);
        else await reloadHistory();
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    }
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [jobId, version.id, pollEpoch]);
  useEffect(() => {
    let alive = true;
    api(`/scripts/${version.id}/features`)
      .then((fs) => {
        if (alive && fs.length) {
          setFeatures(fs);
          setSavedVersion(version.id);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [version.id]);

  async function reloadJob() {
    const row = await api(`/analysis-jobs/${jobId}`);
    setJob(row);
    if (row.result?.draft_id)
      setDraft(await api(`/transformation-drafts/${row.result.draft_id}`));
    await reloadHistory();
  }
  async function start(operation: string) {
    const row = await api(`/scripts/${version.id}/analysis-jobs`, "POST", {
      request_key: requestKey(),
      operation,
      selected_features: operation === "TRANSFORM" ? selected : [],
      use_ai: operation === "TRANSFORM" && useAI,
    });
    chooseJob(row.id);
    await reloadHistory();
  }
  async function loadDiff(file = diffFile, offset = diffOffset) {
    if (!draft) return;
    setDiff(
      await api(
        `/transformation-drafts/${draft.id}/diff?offset=${offset}&limit=120${file ? "&file=" + encodeURIComponent(file) : ""}`,
      ),
    );
    setDiffFile(file);
    setDiffOffset(offset);
  }
  async function review(f: Row, action: string) {
    await api(`/script-features/${f.id}/review`, "POST", {
      action,
      reviewed_sha256: f.review_sha256,
      confirmed: true,
    });
    await reloadJob();
  }
  async function saveDraft() {
    if (!draft) return;
    const saved = await api(`/transformation-drafts/${draft.id}/save`, "POST", {
      feature_ids: draft.features
        .filter((f: Row) => f.status === "APPROVED")
        .map((f: Row) => f.id),
      reviewed_sha256: draft.review_sha256,
      confirmed: true,
    });
    setSavedVersion(saved.id);
    setFeatures(await api(`/scripts/${saved.id}/features`));
    await reloadJob();
    await refresh();
  }
  function chooseFeature(id: string) {
    setTaskFeature(id);
    const f = features.find((f) => f.id === id)?.definition;
    setTaskName(f ? f.name + " · 管理任务" : "");
    setWallet(f?.wallet_input || null);
    const defaults: Row = {};
    f?.parameters.forEach((p: Row) => {
      if (p.has_default && !p.sensitive)
        defaults[p.name] =
          typeof p.default === "object" ? pretty(p.default) : String(p.default);
    });
    setParameters(defaults);
  }
  const chosen = features.find((f) => f.id === taskFeature)?.definition;
  async function createTask() {
    if (!chosen) throw new Error("请选择已审核功能");
    const values: Row = {};
    for (const p of chosen.parameters) {
      const raw = parameters[p.name];
      if (raw === undefined || raw === "") {
        if (p.required) throw new Error("请填写必填参数：" + p.name);
        continue;
      }
      if (p.sensitive) values[p.name] = { reference: raw };
      else if (p.type === "integer" || p.type === "number") {
        values[p.name] = Number(raw);
        if (!Number.isFinite(values[p.name]))
          throw new Error("数值无效：" + p.name);
      } else if (p.type === "boolean") values[p.name] = raw === "true";
      else if (p.type === "array" || p.type === "object")
        values[p.name] = JSON.parse(raw);
      else values[p.name] = raw;
    }
    let project = projectId;
    if (project === "__new__") {
      if (!newProject.trim()) throw new Error("请输入项目名称");
      project = (await api("/projects", "POST", { name: newProject.trim() }))
        .id;
    }
    if (!project || !taskName.trim())
      throw new Error("请填写任务名称并选择项目");
    await api("/tasks", "POST", {
      name: taskName.trim(),
      project_id: project,
      script_version_id: savedVersion,
      script_feature_id: taskFeature,
      feature_parameters: values,
      wallet_input: wallet,
      batch_size: batchSize,
      max_retries: retries,
    });
    setProjectId(project);
    await refresh();
  }
  const candidates: Row[] = job?.result?.features || [];
  const canCancel =
    job && (live.has(job.status) || job.status === "WAITING_FOR_AI_APPROVAL");

  return (
    <div className="smart-analysis">
      <div className="smart-heading">
        <button className="secondary" onClick={back}>
          <ArrowLeft size={15} /> 返回脚本中心
        </button>
        <div>
          <h2>
            智能分析 · {version.name} · v{version.version}
          </h2>
          <p>分析、审核和任务配置。真实执行、签名及链上交易保持关闭。</p>
        </div>
      </div>
      {error && (
        <div className="smart-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="smart-success" role="status">
          <Check size={16} />
          {notice}
        </div>
      )}
      {section(
        "1 · 分析原始源码",
        <>
          <div className="smart-toolbar">
            <button
              className="primary"
              disabled={busy || !!canCancel}
              onClick={() => act(() => start("DISCOVER"))}
            >
              <Sparkles size={15} /> 开始结构发现
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  await reloadHistory();
                  if (jobId) await reloadJob();
                })
              }
            >
              <RefreshCw size={14} /> 刷新状态
            </button>
          </div>
          <p className="muted">
            {cap.worker_online
              ? "分析 Worker 在线"
              : "分析 Worker 暂未在线，任务会保存在队列中"}{" "}
            · 并发 {cap.concurrency || 1} · 受限子进程{" "}
            {cap.memory_limit_mib || "—"} MiB · 总时限{" "}
            {cap.timeout_seconds || "—"} 秒
          </p>
          <label className="field">
            <span>此版本的分析记录</span>
            <select
              aria-label="分析记录"
              value={jobId}
              onChange={(e) => chooseJob(e.target.value)}
            >
              <option value="">选择分析记录</option>
              {history.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.operation === "DISCOVER" ? "结构发现" : "入口重构"} ·{" "}
                  {names[h.status]} ·{" "}
                  {new Date(h.created_at * 1000).toLocaleString("zh-CN")}
                </option>
              ))}
            </select>
          </label>
          {job && (
            <div className="smart-progress" aria-live="polite">
              <strong>{names[job.status]}</strong>
              <span>已启动 {job.attempts} 次；显示实际阶段，不估算百分比</span>
              {canCancel && (
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      await api(`/analysis-jobs/${jobId}/cancel`, "POST");
                      await reloadJob();
                    })
                  }
                >
                  取消当前分析
                </button>
              )}
            </div>
          )}
          {job?.error && (
            <div className="smart-warning">
              {job.error}
              <p>
                原版本保留。可重新开始结构发现；需要 AI 时必须重新审核发送内容。
              </p>
            </div>
          )}
          {job?.result?.errors?.length > 0 && (
            <ul>
              {job?.result.errors.map((e: Row | string, i: number) => (
                <li key={i}>{typeof e === "string" ? e : pretty(e)}</li>
              ))}
            </ul>
          )}
          {job && !canCancel && (
            <details className="smart-cleanup">
              <summary>清理此分析记录</summary>
              <p>
                永久删除此记录、草稿和
                Diff；保留原版本、已保存的新版本及其任务。运行中的分析须先取消。
              </p>
              <label className="smart-check">
                <input
                  type="checkbox"
                  checked={confirmedCleanup}
                  onChange={(e) => setConfirmedCleanup(e.target.checked)}
                />
                我确认清理此分析记录
              </label>
              <button
                className="danger-button"
                disabled={busy || !confirmedCleanup}
                onClick={() =>
                  act(async () => {
                    await api(`/analysis-jobs/${jobId}`, "DELETE", {
                      confirmed: true,
                    });
                    chooseJob("");
                    await reloadHistory();
                  }, "分析记录已清理")
                }
              >
                永久删除分析记录
              </button>
            </details>
          )}
        </>,
      )}
      {!!candidates.length &&
        !draft &&
        section(
          "2 · 选择需要适配的功能",
          <>
            <p>
              候选来自真实入口与调用关系。登录先后顺序、动态依赖和共享会话无法可靠推断时，需要人工确认。
            </p>
            <div className="smart-grid">
              {candidates.map((f) => (
                <Feature
                  key={f.feature_id}
                  feature={f}
                  selected={selected.includes(f.feature_id)}
                  onSelect={() =>
                    setSelected((s) =>
                      s.includes(f.feature_id)
                        ? s.filter((id) => id !== f.feature_id)
                        : [...s, f.feature_id],
                    )
                  }
                />
              ))}
            </div>
            <details>
              <summary>完整结构与交互发现</summary>
              <pre className="smart-pre">
                {pretty({
                  file_tree: job?.result?.file_tree,
                  modules: job?.result?.modules,
                  findings: job?.result?.findings,
                  excluded_data_files: job?.result?.excluded_data_files,
                })}
              </pre>
            </details>
            <label className="smart-check">
              <input
                type="checkbox"
                checked={useAI}
                onChange={(e) => setUseAI(e.target.checked)}
              />
              使用已有 AI Provider 规划有限重构（先预览，后审核发送）
            </label>
            {useAI && !cap.ai_configured && (
              <p className="smart-warning">
                尚未配置 AI。
                <button className="secondary" onClick={settings}>
                  打开系统设置
                </button>
              </p>
            )}
            <button
              className="primary"
              disabled={
                busy ||
                !selected.length ||
                !!canCancel ||
                (useAI && !cap.ai_configured)
              }
              onClick={() => act(() => start("TRANSFORM"))}
            >
              生成所选功能的适配草稿（{selected.length}）
            </button>
          </>,
        )}
      {preview &&
        job?.status === "WAITING_FOR_AI_APPROVAL" &&
        section(
          "3 · 审核 AI 发送内容",
          <>
            <p>
              将发送下方完整脱敏上下文给 {preview.provider?.url}，模型{" "}
              {preview.provider?.model}。只包含所选功能的必要 Python
              片段及分析，不包含钱包私钥或数据文件内容。
            </p>
            <label className="field">
              <span>预览 SHA-256</span>
              <input
                aria-label="预览 SHA-256"
                readOnly
                value={preview.sha256}
              />
            </label>
            <pre className="smart-pre" data-testid="ai-preview">
              {pretty(preview.payload)}
            </pre>
            <label className="smart-check">
              <input
                aria-label="确认 AI 发送内容"
                type="checkbox"
                checked={checkedAI}
                onChange={(e) => setCheckedAI(e.target.checked)}
              />
              我已检查全部上下文，并同意向上述 Provider 发送该摘要对应内容
            </label>
            <button
              className="primary"
              disabled={busy || !checkedAI}
              onClick={() =>
                act(async () => {
                  await api(`/analysis-jobs/${jobId}/approve-ai`, "POST", {
                    reviewed_preview_sha256: preview.sha256,
                    confirmed: true,
                  });
                  setPreview(null);
                  setPollEpoch((n) => n + 1);
                }, "已批准发送；正在等待受限重构")
              }
            >
              批准此内容并继续
            </button>
          </>,
        )}
      {draft &&
        section(
          "4 · 审核草稿与逐项功能",
          <>
            <div className="smart-facts">
              <p>新增：{draft.summary.new_files?.join("、") || "无"}</p>
              <p>修改：{draft.summary.modified_files?.join("、") || "无"}</p>
              <p>
                新增依赖：{draft.summary.new_dependencies?.join("、") || "无"}
              </p>
            </div>
            {draft.summary.unresolved?.length > 0 && (
              <div className="smart-warning">
                {draft.summary.unresolved.join("；")}
              </div>
            )}
            <button
              className="secondary"
              disabled={busy}
              onClick={() => act(() => loadDiff())}
            >
              {diff ? "重新加载 Diff" : "查看分页 Diff"}
            </button>
            {diff && (
              <div className="smart-diff">
                <label className="field">
                  <span>选择 Diff 文件</span>
                  <select
                    value={diffFile}
                    onChange={(e) => act(() => loadDiff(e.target.value, 0))}
                  >
                    <option value="">全部文件</option>
                    {(diffFile
                      ? draft.summary.new_files?.concat(
                          draft.summary.modified_files || [],
                        ) || []
                      : diff.files
                    ).map((f: string) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                </label>
                <pre>{diff.lines.join("\n")}</pre>
                <div className="smart-toolbar">
                  <button
                    className="secondary"
                    disabled={busy || !diffOffset}
                    onClick={() =>
                      act(() =>
                        loadDiff(diffFile, Math.max(0, diffOffset - 120)),
                      )
                    }
                  >
                    上一页
                  </button>
                  <span>
                    第 {diffOffset + 1}–
                    {Math.min(diffOffset + 120, diff.total_lines)} 行 / 共{" "}
                    {diff.total_lines} 行
                  </span>
                  <button
                    className="secondary"
                    disabled={busy || !diff.more}
                    onClick={() =>
                      act(() => loadDiff(diffFile, diffOffset + 120))
                    }
                  >
                    下一页
                  </button>
                </div>
              </div>
            )}
            {!draft.result_version_id && (
              <label className="smart-check">
                <input
                  aria-label="确认已审核 Diff"
                  type="checkbox"
                  checked={checkedDiff}
                  disabled={!diff}
                  onChange={(e) => setCheckedDiff(e.target.checked)}
                />
                我已审阅全部相关 Diff、配置和风险，按功能作出决定
              </label>
            )}
            <div className="smart-grid">
              {draft.features.map((f: Row) => (
                <Feature key={f.id} feature={f}>
                  {!draft.result_version_id && (
                    <div className="smart-toolbar">
                      <button
                        className="primary"
                        disabled={
                          busy ||
                          !checkedDiff ||
                          !!f.definition.blocking_reasons.length ||
                          !f.definition.entrypoint ||
                          f.status === "APPROVED"
                        }
                        onClick={() => act(() => review(f, "APPROVED"))}
                      >
                        批准功能 {f.definition.name}
                      </button>
                      <button
                        className="secondary"
                        disabled={
                          busy || !checkedDiff || f.status === "REJECTED"
                        }
                        onClick={() => act(() => review(f, "REJECTED"))}
                      >
                        拒绝此功能
                      </button>
                    </div>
                  )}
                </Feature>
              ))}
            </div>
            {!draft.result_version_id && (
              <button
                className="primary"
                disabled={
                  busy ||
                  !checkedDiff ||
                  !draft.features.some((f: Row) => f.status === "APPROVED")
                }
                onClick={() =>
                  act(saveDraft, "已保存审核通过的功能为新版本，原版本保留")
                }
              >
                将已批准功能保存为新版本
              </button>
            )}
            {draft.result_version_id && (
              <p className="smart-success">
                新版本已保存。下方可创建独立功能任务。
              </p>
            )}
          </>,
        )}
      {features.length > 0 &&
        section(
          "5 · 创建功能级管理任务",
          <>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                act(
                  createTask,
                  "功能任务已保存，可在项目中心查看；执行保持关闭",
                );
              }}
            >
              <div className="smart-form-grid">
                <label className="field">
                  <span>已审核功能</span>
                  <select
                    required
                    aria-label="已审核功能"
                    value={taskFeature}
                    onChange={(e) => chooseFeature(e.target.value)}
                  >
                    <option value="">请选择一个功能</option>
                    {features.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.definition.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>项目</span>
                  <select
                    required
                    aria-label="功能任务项目"
                    value={projectId}
                    onChange={(e) => setProjectId(e.target.value)}
                  >
                    <option value="">选择项目</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                    <option value="__new__">＋ 新建项目</option>
                  </select>
                </label>
                {projectId === "__new__" && (
                  <label className="field">
                    <span>新项目名称</span>
                    <input
                      aria-label="新项目名称"
                      required
                      maxLength={100}
                      value={newProject}
                      onChange={(e) => setNewProject(e.target.value)}
                    />
                  </label>
                )}
                <label className="field">
                  <span>任务名称</span>
                  <input
                    aria-label="功能任务名称"
                    required
                    maxLength={200}
                    value={taskName}
                    onChange={(e) => setTaskName(e.target.value)}
                  />
                </label>
                <label className="field">
                  <span>自动关联入口</span>
                  <input
                    readOnly
                    aria-label="自动关联入口"
                    value={chosen?.entrypoint || "选择功能后自动关联"}
                  />
                </label>
              </div>
              <div className="smart-form-grid">
                <label className="field">
                  <span>批次大小（配置）</span>
                  <input
                    aria-label="功能批次大小"
                    type="number"
                    required
                    min={1}
                    max={1000}
                    value={batchSize}
                    onChange={(e) => setBatchSize(Number(e.target.value))}
                  />
                </label>
                <label className="field">
                  <span>最大重试次数（配置）</span>
                  <input
                    aria-label="功能最大重试次数"
                    type="number"
                    required
                    min={0}
                    max={3}
                    value={retries}
                    onChange={(e) => setRetries(Number(e.target.value))}
                  />
                </label>
              </div>
              {chosen?.parameters.map((p: Row) => (
                <label key={p.name} className="field">
                  <span>
                    {p.name} {p.required ? "（必填）" : "（可省略）"}
                    {p.sensitive && " · 凭证引用名称"}
                  </span>
                  {p.type === "boolean" && !p.sensitive ? (
                    <select
                      required={p.required}
                      aria-label={`参数 ${p.name}`}
                      value={parameters[p.name] || ""}
                      onChange={(e) =>
                        setParameters((v) => ({
                          ...v,
                          [p.name]: e.target.value,
                        }))
                      }
                    >
                      <option value="">未设置</option>
                      <option value="true">是</option>
                      <option value="false">否</option>
                    </select>
                  ) : (
                    <input
                      aria-label={`参数 ${p.name}`}
                      required={p.required}
                      value={parameters[p.name] ?? ""}
                      type={
                        !p.sensitive && ["integer", "number"].includes(p.type)
                          ? "number"
                          : "text"
                      }
                      step={p.type === "integer" ? 1 : "any"}
                      min={p.constraints.minimum}
                      max={p.constraints.maximum}
                      minLength={p.constraints.minLength}
                      maxLength={Math.min(
                        1000,
                        p.constraints.maxLength || 1000,
                      )}
                      onChange={(e) =>
                        setParameters((v) => ({
                          ...v,
                          [p.name]: e.target.value,
                        }))
                      }
                    />
                  )}
                  <small>
                    {p.sensitive
                      ? "只填写引用名称；不要填写私钥、密码或 token。"
                      : p.validation}{" "}
                    · 来源 {p.source}
                    {p.has_default && !p.sensitive
                      ? " · 源码默认值 " + pretty(p.default)
                      : ""}
                  </small>
                </label>
              ))}
              {wallet && (
                <details>
                  <summary>钱包输入映射</summary>
                  <p>
                    自动采用分析结果。仅配置来源与字段角色，实际凭证不可用于此适配器。
                  </p>
                  <label className="field">
                    <span>来源格式</span>
                    <select
                      aria-label="钱包来源格式"
                      value={wallet.mode}
                      onChange={(e) =>
                        setWallet((w) => ({ ...w, mode: e.target.value }))
                      }
                    >
                      {[
                        "unknown",
                        "CSV",
                        "TXT",
                        "JSON",
                        "XLSX",
                        "environment",
                        "cli",
                        "custom",
                      ].map((m) => (
                        <option key={m}>{m}</option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>来源文件或配置名称</span>
                    <input
                      maxLength={300}
                      value={wallet.source}
                      onChange={(e) =>
                        setWallet((w) => ({ ...w, source: e.target.value }))
                      }
                    />
                  </label>
                  {Object.entries(wallet.fields).map(([name, role]) => (
                    <label key={name} className="field">
                      <span>{name}</span>
                      <select
                        value={String(role)}
                        onChange={(e) =>
                          setWallet((w) => ({
                            ...w,
                            fields: { ...w!.fields, [name]: e.target.value },
                          }))
                        }
                      >
                        {[
                          "identity",
                          "credential",
                          "account_config",
                          "project_config",
                          "feature_parameter",
                        ].map((r) => (
                          <option key={r}>{r}</option>
                        ))}
                      </select>
                    </label>
                  ))}
                  <small>
                    身份字段、凭证引用、账户配置、项目配置与业务参数分别保存。配置中无需提供真实钱包文件内容。
                  </small>
                </details>
              )}
              <button
                className="primary"
                type="submit"
                disabled={busy || !taskFeature}
              >
                保存功能任务配置
              </button>
            </form>
          </>,
        )}
    </div>
  );
}
