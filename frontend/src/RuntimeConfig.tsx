import React, { useEffect, useMemo, useState } from "react";
import {
  Boxes,
  Check,
  Play,
  RefreshCw,
  ShieldAlert,
  Sparkles,
} from "lucide-react";

type Row = Record<string, any>;

const labels: Row = {
  DRAFT: "待运行审核",
  APPROVED: "运行审核已通过",
  PENDING: "等待构建",
  RUNNING: "构建中",
  READY: "环境就绪",
  FAILED: "构建失败",
  DETECTED: "待运行审核",
  UPLOADED: "待适配",
};

function Badge({ status }: { status: string }) {
  const color = ["APPROVED", "READY"].includes(status)
    ? "green"
    : ["FAILED"].includes(status)
      ? "red"
      : ["RUNNING", "PENDING"].includes(status)
        ? "blue"
        : "gray";
  return (
    <span className={"badge " + color}>
      <i />
      {labels[status] || status || "尚无运行定义"}
    </span>
  );
}

const pretty = (value: any) => JSON.stringify(value ?? {}, null, 2);

const defaultSpec = (feature: Row) => ({
  protocol: "airdrop-native-runtime-v1",
  entrypoint: feature?.definition?.entrypoint || "main.py",
  workdir: "",
  python_version: "3.12",
  dependency_files: [],
  arguments: [],
  parameters: [],
  configuration_bindings: [],
  wallet_binding: null,
  state_bindings: [],
  state_schema_version: 1,
  capabilities: [],
  execution_mode: "script_managed",
  result_mode: "logs_only",
  stop_signal: "SIGINT",
  stop_grace_seconds: 60,
  unresolved: [],
});

export default function RuntimeConfig({
  feature,
  api,
  projects = [],
  wallets = [],
  refresh,
  compact = false,
  reviewOnly = false,
}: {
  feature: Row;
  api: (path: string, method?: string, body?: any) => Promise<any>;
  projects?: Row[];
  wallets?: Row[];
  refresh?: () => Promise<any> | void;
  compact?: boolean;
  reviewOnly?: boolean;
}) {
  const [view, setView] = useState<Row | null>(null);
  const [text, setText] = useState("");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [projectId, setProjectId] = useState("");
  const [walletIds, setWalletIds] = useState<string[]>([]);
  const [parameters, setParameters] = useState<Row>({});
  const [taskName, setTaskName] = useState("");
  const [capabilities, setCapabilities] = useState<Row>({});

  async function act(fn: () => Promise<any>, message = "") {
    setBusy(true);
    setError("");
    try {
      const result = await fn();
      if (message) setNotice(result?.warning || message);
      return result;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function load() {
    const [runtime, caps] = await Promise.all([
      api(`/script-features/${feature.id}/runtime`),
      api("/runtime/capabilities"),
    ]);
    setView(runtime);
    setCapabilities(caps);
    setText(pretty(runtime.runtime_spec || defaultSpec(feature)));
    setChecked(false);
    return runtime;
  }

  useEffect(() => {
    load().catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feature.id]);

  const building = ["PENDING", "RUNNING"].includes(view?.environment?.status);
  useEffect(() => {
    if (!building) return;
    const timer = setTimeout(() => {
      load().catch((e) => setError(e.message));
    }, 2500);
    return () => clearTimeout(timer);
  }, [building, view?.environment?.status, view?.runtime_build_id]);

  const spec = view?.runtime_spec || null;
  const readiness: string[] = view?.readiness || [];
  const credentials = useMemo(
    () =>
      Object.entries(spec?.wallet_binding?.fields || {})
        .filter(([, role]) => role === "credential")
        .map(([field]) => field),
    [spec],
  );
  const parameterList: Row[] = spec?.parameters || [];
  const runnable =
    view?.runtime_status === "APPROVED" &&
    view?.environment?.status === "READY" &&
    readiness.length === 0;
  const availableWallets = wallets.filter(
    (w: Row) =>
      !spec?.wallet_binding?.chain || w.chain === spec.wallet_binding.chain,
  );

  async function save() {
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError("运行定义不是合法 JSON，请修正后再提交。");
      return;
    }
    const runtime = await act(
      () =>
        api(`/script-features/${feature.id}/runtime`, "PUT", {
          runtime: parsed,
          confirmed: true,
        }),
      "运行定义已提交；此前的运行审核已失效，请重新审核。",
    );
    if (runtime) await load();
    await refresh?.();
  }

  async function review() {
    const result = await act(
      () =>
        api(`/script-features/${feature.id}/runtime-review`, "POST", {
          reviewed_sha256: view?.review_sha256,
          confirmed: true,
        }),
      "运行审核已通过，可以准备依赖环境。",
    );
    if (result) await load();
    await refresh?.();
  }

  async function prepare() {
    const result = await act(
      () => api("/runtime-builds", "POST", { feature_id: feature.id }),
      "",
    );
    if (result) {
      setNotice(
        result.reused
          ? "已复用同一环境键的成功镜像，不会重复安装依赖。"
          : "已开始准备依赖环境；构建不携带任何账号输入。",
      );
      await load();
    }
    await refresh?.();
  }

  async function createTask() {
    const missing = parameterList.filter(
      (p: Row) =>
        p.required &&
        (parameters[p.name] === undefined || parameters[p.name] === ""),
    );
    if (missing.length) {
      setError("请填写必填参数：" + missing.map((p) => p.name).join("、"));
      return;
    }
    if (!projectId) {
      setError("请选择任务所属项目。");
      return;
    }
    if (!walletIds.length) {
      setError("原生运行必须在配置任务时明确选择本次钱包。");
      return;
    }
    const values: Row = {};
    for (const p of parameterList) {
      const raw = parameters[p.name];
      if (raw === undefined || raw === "") continue;
      if (p.type === "integer" || p.type === "number") values[p.name] = Number(raw);
      else if (p.type === "boolean") values[p.name] = raw === "true";
      else if (p.type === "array" || p.type === "object") {
        try {
          values[p.name] = JSON.parse(raw);
        } catch {
          setError(`参数 ${p.name} 需要合法 JSON。`);
          return;
        }
      } else values[p.name] = raw;
    }
    const result = await act(
      () =>
        api("/tasks", "POST", {
          name: taskName.trim() || feature.definition?.name || "原生运行任务",
          project_id: projectId,
          script_version_id: feature.version_id,
          script_feature_id: feature.id,
          feature_parameters: values,
          execution_backend: "native",
          wallet_ids: walletIds,
          batch_size: 100,
          max_retries: 0,
        }),
      "原生运行任务已保存。执行时会按运行定义写入所选钱包，日志在重新打开页面后仍可续读。",
    );
    if (result) {
      setWalletIds([]);
      await refresh?.();
    }
  }

  return (
    <div className={"runtime-config" + (compact ? " compact" : "")}>
      <div className="runtime-head">
        <Badge status={view?.runtime_status || ""} />
        <span className="muted">
          运行定义变更或依赖摘要变化都会让运行审核失效
        </span>
        <button onClick={() => act(load)} disabled={busy}>
          <RefreshCw size={13} />
          刷新
        </button>
      </div>
      {notice && (
        <div className="hint" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {!reviewOnly && (
        <div className="runtime-flags">
        <span className={capabilities.native_execution_enabled ? "" : "muted"}>
          原生执行开关：
          {capabilities.native_execution_enabled ? "已启用" : "未启用"}
        </span>
        <span className={capabilities.controller?.available ? "" : "danger-text"}>
          执行控制器：
          {capabilities.controller?.available
            ? "在线"
            : capabilities.controller?.reason || "不可用"}
        </span>
        <span>
          运行限额：超时 {capabilities.limits?.timeout_seconds ?? "—"} 秒 · 内存{" "}
          {capabilities.limits?.memory_mib ?? "—"} MiB · CPU{" "}
          {capabilities.limits?.cpus ?? "—"} · 全局并发{" "}
          {capabilities.limits?.global_concurrency ?? "—"}
        </span>
        <span>
          钱包来源：{capabilities.wallet_source || "本次所选钱包按运行定义写入脚本原文件字段"}
        </span>
        </div>
      )}
      {spec?.capabilities?.includes("selected_wallet_private_keys") && (
        <div className="hint wallet-warning">
          <ShieldAlert size={15} />
          <span>
            运行定义声明会把本次所选钱包的私钥写入脚本读取的文件字段（
            {credentials.join("、") || "未列出"}）。
            脚本因此具备签名能力，平台不做签名代理，也不会自动确认资金操作。
          </span>
        </div>
      )}
      {readiness.length > 0 && (
        <div className="smart-warning">
          <strong>尚未解决</strong>
          <ul>
            {readiness.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      <label className="field">
        <span>运行定义（airdrop-native-runtime-v1）</span>
        <textarea
          aria-label="运行定义 JSON"
          className="runtime-json"
          value={text}
          spellCheck={false}
          onChange={(e) => {
            setText(e.target.value);
            setChecked(false);
          }}
        />
        <small>
          入口、钱包文件、状态文件与参数交付位置都在这里声明；保存后会生成新的审核摘要。
        </small>
      </label>
      <div className="runtime-actions">
        <button className="secondary" disabled={busy} onClick={save}>
          <Check size={13} />
          保存运行定义
        </button>
        <label className="checkbox">
          <input
            type="checkbox"
            aria-label="确认运行审核摘要"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
          />
          我已核对入口、私钥交付与状态回写
        </label>
        <button
          className="primary"
          disabled={busy || !checked || !view?.review_sha256}
          title={
            view?.review_sha256
              ? "按精确摘要确认运行审核"
              : "请先保存运行定义"
          }
          onClick={review}
        >
          通过运行审核
        </button>
      </div>
      <div className="runtime-digest">
        <span>当前审核摘要</span>
        <code title={view?.review_sha256}>{view?.review_sha256 || "尚未生成"}</code>
      </div>
      {!reviewOnly && (
        <>
      <div className="runtime-actions">
        <button
          disabled={busy || !spec}
          title={spec ? "按环境键构建或复用镜像" : "请先提交运行定义"}
          onClick={prepare}
        >
          <Boxes size={13} />
          准备依赖环境
        </button>
        {view?.environment && (
          <span className="muted">
            环境键状态：<Badge status={view.environment.status} /> · 镜像{" "}
            {view.environment.image_id ? view.environment.image_id.slice(0, 20) + "…" : "尚未生成"}
          </span>
        )}
        {view?.environment?.status === "FAILED" && (
          <button
            className="danger-button"
            disabled={busy}
            onClick={async () => {
              await act(
                () =>
                  api(
                    `/runtime-builds/${view.environment.id}/retry`,
                    "POST",
                  ),
                "已重新提交构建。",
              );
              await load();
            }}
          >
            重新构建
          </button>
        )}
      </div>
      {view?.environment?.error && (
        <div className="error">{view.environment.error}</div>
      )}
      {view?.environment?.log && (
        <details>
          <summary>构建日志</summary>
          <pre>{view.environment.log}</pre>
        </details>
      )}
      <details className="runtime-task" open={!compact}>
        <summary>
          <Sparkles size={13} />
          配置原生运行任务
        </summary>
        <p>
          任务固定本次钱包选择与非敏感参数；运行时平台按运行定义生成脚本原文件，
          私钥只进入这次运行的工作目录。管理模式下无法创建或运行，请先确认上方能力开关。
        </p>
        <div className="smart-form-grid">
          <label className="field">
            <span>项目</span>
            <select
              aria-label="原生任务项目"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
            >
              <option value="">选择项目</option>
              {projects.map((p: Row) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>任务名称</span>
            <input
              aria-label="原生任务名称"
              maxLength={200}
              value={taskName}
              placeholder={feature.definition?.name || "原生运行任务"}
              onChange={(e) => setTaskName(e.target.value)}
            />
          </label>
        </div>
        <div className="field">
          <span>
            本次钱包（{walletIds.length}/{availableWallets.length}）
            {spec?.wallet_binding?.chain
              ? ` · 只列出 ${spec.wallet_binding.chain} 钱包`
              : ""}
          </span>
          <div className="check-list wallet-picks">
            {availableWallets.map((w: Row) => (
              <label className="check-row" key={w.id}>
                <input
                  type="checkbox"
                  aria-label={`选择钱包 ${w.name || w.address}`}
                  checked={walletIds.includes(w.id)}
                  onChange={(e) =>
                    setWalletIds((ids) =>
                      e.target.checked
                        ? [...ids, w.id]
                        : ids.filter((id) => id !== w.id),
                    )
                  }
                />
                <code title={w.address}>
                  {(w.address || "").slice(0, 12)}…
                </code>
                <span>{w.name || "未命名"}</span>
                <small>{w.chain}</small>
              </label>
            ))}
            {!availableWallets.length && (
              <p className="muted">没有符合运行定义链类型的钱包，请先在钱包中心创建。</p>
            )}
          </div>
          <small>
            平台只按当次明确选择生成钱包文件，不默认运行全部导入账号。
          </small>
        </div>
        {parameterList.map((p: Row) => (
          <label className="field" key={p.name}>
            <span>
              {p.name}
              {p.required ? "（必填）" : "（可省略）"} · {p.type}
            </span>
            {p.type === "boolean" ? (
              <select
                aria-label={`原生参数 ${p.name}`}
                value={parameters[p.name] ?? ""}
                onChange={(e) =>
                  setParameters((v) => ({ ...v, [p.name]: e.target.value }))
                }
              >
                <option value="">未设置</option>
                <option value="true">是</option>
                <option value="false">否</option>
              </select>
            ) : (
              <input
                aria-label={`原生参数 ${p.name}`}
                value={parameters[p.name] ?? ""}
                onChange={(e) =>
                  setParameters((v) => ({ ...v, [p.name]: e.target.value }))
                }
              />
            )}
            {p.description && <small>{p.description}</small>}
          </label>
        ))}
        <button
          className="primary"
          disabled={busy || !spec || !runnable}
          title={
            runnable
              ? "保存原生运行任务"
              : "运行定义未通过运行审核、环境未就绪或仍有未解决项"
          }
          onClick={createTask}
        >
          <Play size={13} />
          保存原生运行任务
        </button>
      </details>
        </>
      )}
    </div>
  );
}
