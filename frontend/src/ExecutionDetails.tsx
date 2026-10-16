import React, { useEffect, useMemo, useState } from "react";
import {
  Download,
  Play,
  RefreshCw,
  ShieldAlert,
  Square,
  Wrench,
} from "lucide-react";
import ExecutionLog, { downloadBlob } from "./ExecutionLog";

type Row = Record<string, any>;

const labels: Row = {
  SUCCESS: "成功",
  FAILED: "失败",
  PARTIAL_SUCCESS: "部分成功",
  TIMEOUT: "超时",
  CANCELLED: "已取消",
  QUEUED: "排队中",
  PREPARING: "准备中",
  RUNNING: "运行中",
  STOPPING: "停止中",
  RECONCILING: "核对中",
  COMPLETED: "进程已结束",
  UNKNOWN: "结果未知",
  MANUAL_REQUIRED: "需人工核查",
  NO_REPORT: "未收到单钱包结果",
};

const STATE_LABELS: Row = {
  "": "未涉及状态回写",
  PENDING: "等待状态收集",
  LOCKED: "状态空间已锁定",
  COLLECTING: "正在收集状态",
  COMMITTED: "状态已回写",
  PENDING_REVIEW: "状态候选待人工复核",
  UNCOLLECTED: "状态未收集",
  FAILED: "状态回写失败",
};

const ACTIVE = new Set(["QUEUED", "PREPARING", "RUNNING", "STOPPING", "RECONCILING"]);
const REVIEW = new Set(["UNKNOWN", "MANUAL_REQUIRED"]);

const fmt = (value?: number | null) =>
  value ? new Date(value * 1000).toLocaleString("zh-CN") : "—";

const short = (value?: string, size = 14) =>
  value && value.length > size ? value.slice(0, size) + "…" : value || "—";

function Badge({ status }: { status: string }) {
  const color = ["SUCCESS", "COMPLETED", "ONLINE", "ALREADY_DONE"].includes(status)
    ? "green"
    : ["FAILED", "UNKNOWN", "TIMEOUT", "MANUAL_REQUIRED", "OFFLINE"].includes(status)
      ? "red"
      : ["RUNNING", "QUEUED", "PREPARING", "STOPPING", "RECONCILING"].includes(status)
        ? "blue"
        : "gray";
  return (
    <span className={"badge " + color}>
      <i />
      {labels[status] || status}
    </span>
  );
}

/** 原生运行请求键：与后端幂等键同格式，重复点击不会创建第二个运行。 */
export function newRequestKey() {
  return (
    crypto.randomUUID().replaceAll("-", "") +
    Math.floor(Math.random() * 1_000_000)
      .toString(36)
      .padStart(4, "0")
  ).slice(0, 64);
}

export function canRunNative(capabilities: Row, task?: Row) {
  if (!task || task.execution_backend !== "native") {
    return { allowed: false, reason: "该任务不是原生运行任务" };
  }
  if (capabilities.native_execution_enabled !== true) {
    return { allowed: false, reason: "原生执行开关未启用（AIRDROP_NATIVE_EXECUTION_ENABLED）" };
  }
  if (!capabilities.controller?.available) {
    return {
      allowed: false,
      reason: capabilities.controller?.reason || "执行控制器不可用",
    };
  }
  if (!(task.wallet_selection?.wallet_ids || []).length) {
    return { allowed: false, reason: "任务未选择本次钱包，请重新配置任务" };
  }
  return { allowed: true, reason: "" };
}

export default function ExecutionDetails({
  executionId,
  api,
  tasks = [],
  capabilities = {},
  onRun,
  onClose,
  onChanged,
}: {
  executionId: string;
  api: (path: string, method?: string, body?: any) => Promise<any>;
  tasks?: Row[];
  capabilities?: Row;
  onRun?: (task: Row) => Promise<any>;
  onClose?: () => void;
  onChanged?: () => void;
}) {
  const [detail, setDetail] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<Row | null>(null);
  const [values, setValues] = useState<Row>({});
  const [formError, setFormError] = useState("");

  const active = ACTIVE.has(detail?.status || "");
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function update() {
      try {
        const row = await api(`/executions/${executionId}`);
        if (!alive) return;
        setDetail(row);
        setError("");
        // 运行中约 1.5 秒刷新；空闲时降到 12 秒，不制造无意义的请求。
        timer = setTimeout(update, ACTIVE.has(row.status) ? 1500 : 12000);
      } catch (e) {
        if (!alive) return;
        setError((e as Error).message);
        timer = setTimeout(update, 8000);
      }
    }
    update();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [executionId]);

  const task = useMemo(
    () => tasks.find((t: Row) => t.id === detail?.task_id),
    [tasks, detail?.task_id],
  );
  const frozen = detail?.runtime_snapshot || null;
  const spec = frozen?.runtime || {};
  const credentials = Object.entries(spec.wallet_binding?.fields || {})
    .filter(([, role]) => role === "credential")
    .map(([field]) => field);
  const native = detail?.execution_backend === "native";
  const runnable = canRunNative(capabilities, task);
  const counted = Object.values(detail?.counts || {}).reduce(
    (sum: number, value: any) => sum + Number(value || 0),
    0,
  );
  const reported = Object.entries(detail?.counts || {}).filter(
    ([status]) => status !== "NO_REPORT" && status !== "QUEUED",
  );
  const noReport = Number(detail?.counts?.NO_REPORT || 0);
  const exitCode = detail?.attempts?.[0]?.process_exit_code;

  async function act(fn: () => Promise<any>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      setDetail(await api(`/executions/${executionId}`));
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function reconcileForm() {
    setValues({
      confirm_container_stopped: false,
      confirm_mock_runner_stopped: false,
      note: "",
    });
    setFormError("");
    setOpen({ kind: "reconcile" });
  }

  function stateReviewForm() {
    setValues({ confirm_candidate_reviewed: false, note: "" });
    setFormError("");
    setOpen({ kind: "state-review" });
  }

  return (
    <section className="panel exec-detail">
      <div className="panel-title">
        <h2>
          执行详情 · {detail?.task_name || "读取中"}
          {detail && (
            <>
              {" "}
              <Badge status={detail.status} />
            </>
          )}
        </h2>
        <div className="actions">
          {detail && ACTIVE.has(detail.status) && (
            <button
              className="danger-button"
              disabled={busy}
              onClick={() =>
                act(() =>
                  api(`/executions/${executionId}/cancel`, "POST").then(() =>
                    Promise.resolve(),
                  ),
                )
              }
            >
              <Square size={13} />
              停止
            </button>
          )}
          {detail && REVIEW.has(detail.status) && (
            <button disabled={busy} onClick={reconcileForm}>
              <Wrench size={13} />
              人工核查
            </button>
          )}
          {detail?.state_status === "PENDING_REVIEW" &&
            detail.state_review_candidate_sha256 && (
              <>
                <button
                  disabled={busy}
                  onClick={() =>
                    act(async () => {
                      const blob = await api(
                        `/executions/${executionId}/state-candidate`,
                      );
                      downloadBlob(
                        blob as Blob,
                        `airdrop-${executionId}-state-candidate.zip`,
                      );
                    })
                  }
                >
                  <Download size={13} />
                  下载状态候选
                </button>
                <button disabled={busy} onClick={stateReviewForm}>
                  <Wrench size={13} />
                  复核并合并
                </button>
              </>
            )}
          {native && task && (
            <button
              disabled={busy || !runnable.allowed}
              title={runnable.allowed ? "按新请求键再次运行" : runnable.reason}
              onClick={() => onRun?.(task)}
            >
              <Play size={13} />
              再次运行
            </button>
          )}
          {detail && (
            <button
              disabled={busy}
              onClick={() =>
                act(async () => {
                  setDetail(await api(`/executions/${executionId}`));
                })
              }
            >
              <RefreshCw size={13} />
              刷新
            </button>
          )}
          {onClose && (
            <button aria-label="关闭执行详情" onClick={onClose}>
              关闭
            </button>
          )}
        </div>
      </div>
      <div className="exec-detail-body">
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        {!detail && <p className="muted">正在读取执行记录…</p>}
        {detail && (
          <>
            <div className="exec-chips">
              <span>脚本版本 {short(detail.script_version_id, 10)}</span>
              <span>
                后端 {native ? "原生容器运行" : "旧模拟 Runner"}
              </span>
              <span>
                周期{" "}
                {detail.period.startsWith("test:")
                  ? "独立测试周期"
                  : detail.period}
              </span>
              <span>开始 {fmt(detail.started_at || detail.created_at)}</span>
              <span>结束 {fmt(detail.finished_at)}</span>
              {native && <span>状态代次 {detail.state_generation ?? "—"}</span>}
            </div>
            {detail.error && <div className="error">{detail.error}</div>}
            {credentials.length > 0 && (
              <div className="hint wallet-warning">
                <ShieldAlert size={15} />
                <span>
                  本次运行把所选钱包的私钥写入了脚本读取的原文件字段（
                  {credentials.join("、")}
                  ）。平台只按运行定义生成该文件，不做签名代理；请确认脚本可信。
                </span>
              </div>
            )}
            {!native && (
              <p className="muted">
                旧模拟路径的日志只保留短尾部并存在 1 MiB 输出上限，与原生运行不同。
              </p>
            )}
            <h3>钱包结果</h3>
            {(detail.results || []).length === 0 ? (
              <div className="empty">
                <strong>没有钱包结果</strong>
                <p>该执行没有登记钱包任务结果。</p>
              </div>
            ) : reported.length === 0 ? (
              <div className="hint" role="status">
                {`本条记录里每个钱包都是“${labels.NO_REPORT}”（共 ${counted} 个）。进程退出码 ${
                  exitCode ?? "未知"
                } 只能说明进程结束，平台不会据此推断钱包业务是否成功。`}
              </div>
            ) : (
              <p className="muted">
                {`结构化结果 ${reported
                  .map(([key, value]) => `${labels[key] || key} ${value}`)
                  .join("、")}；其余 ${noReport} 个钱包没有回传结果。`}
              </p>
            )}
            <div className="results-scroll">
              <table>
                <thead>
                  <tr>
                    <th>钱包</th>
                    <th>状态</th>
                    <th>积分</th>
                    <th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.results || []).map((r: Row) => (
                    <tr key={r.wallet_id}>
                      <td>
                        <code title={r.address}>{short(r.address)}</code>
                        <small>{r.name}</small>
                      </td>
                      <td>
                        <Badge status={r.status} />
                      </td>
                      <td>{r.points ?? "—"}</td>
                      <td>{r.message || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3>运行尝试</h3>
            {(detail.attempts || []).length === 0 && (
              <p className="muted">还没有运行尝试记录。</p>
            )}
            {(detail.attempts || []).map((a: Row) => (
              <div className="attempt-block" key={a.id}>
                <div className="attempt-head">
                  <strong>第 {a.number} 次尝试</strong>
                  <Badge status={a.status} />
                  <span>进程退出码 {a.process_exit_code ?? "尚未退出"}</span>
                  <span>
                    日志 {a.log_bytes ?? 0} 字节 · 开始 {fmt(a.started_at)}
                  </span>
                </div>
                {native ? (
                  <ExecutionLog
                    executionId={executionId}
                    attemptId={a.id}
                    status={detail.status}
                    logStatus={detail.log_status}
                    compact
                  />
                ) : (
                  <pre>{a.log || "旧模拟路径没有日志内容。"}</pre>
                )}
              </div>
            ))}
            {frozen && (
              <details className="exec-frozen">
                <summary>参数快照与固定后的运行引用</summary>
                <div className="exec-frozen-grid">
                  <div>
                    <span>功能 ID</span>
                    <code>{frozen.feature_id || "—"}</code>
                  </div>
                  <div>
                    <span>运行审核摘要</span>
                    <code title={frozen.runtime_review_sha256}>
                      {short(frozen.runtime_review_sha256, 20)}
                    </code>
                  </div>
                  <div>
                    <span>环境构建 / 镜像</span>
                    <code>
                      {short(frozen.runtime_build_id, 10)} /{" "}
                      {short(frozen.image_id, 20)}
                    </code>
                  </div>
                  <div>
                    <span>本次钱包</span>
                    <code>{(frozen.wallet_ids || []).length} 个</code>
                  </div>
                  <div>
                    <span>入口 / 工作目录</span>
                    <code>
                      {spec.entrypoint || "—"}
                      {spec.workdir ? " @" + spec.workdir : ""}
                    </code>
                  </div>
                  <div>
                    <span>超时 / 停止信号</span>
                    <code>
                      {spec.timeout_seconds ?? "默认"} 秒 /{" "}
                      {spec.stop_signal || "SIGINT"}
                    </code>
                  </div>
                </div>
                <h3>固定参数</h3>
                <pre>{JSON.stringify(frozen.parameters || {}, null, 2)}</pre>
              </details>
            )}
            <details className="exec-frozen">
              <summary>状态回写与日志完整性</summary>
              <div className="exec-frozen-grid">
                <div>
                  <span>状态空间</span>
                  <code>
                    {STATE_LABELS[detail.state_status || ""] ||
                      detail.state_status ||
                      "未涉及"}
                  </code>
                </div>
                <div>
                  <span>状态代次</span>
                  <code>{detail.state_generation ?? "—"}</code>
                </div>
                <div>
                  <span>日志状态</span>
                  <code>{detail.log_status || "未记录"}</code>
                </div>
                <div>
                  <span>幂等请求键</span>
                  <code>{detail.request_key || "—"}</code>
                </div>
                {detail.state_review_candidate_sha256 && (
                  <div>
                    <span>状态候选 SHA-256</span>
                    <code>{detail.state_review_candidate_sha256}</code>
                  </div>
                )}
              </div>
              {detail.state_status === "PENDING_REVIEW" && (
                <p className="warning-text">
                  状态产物已加密保留。下载并核对候选后，可将本次所选钱包的数据合并到最新状态；其他钱包记录会保留。
                </p>
              )}
              {REVIEW.has(detail.status) && (
                <p className="warning-text">
                  结果未知的记录保留钱包锁与状态锁，必须人工核对容器后再解除；平台不会自动重放。
                </p>
              )}
            </details>
          </>
        )}
      </div>
      {open?.kind === "reconcile" && (
        <div className="overlay" role="dialog" aria-label="人工核查未知执行">
          <div className="modal exec-modal">
            <div className="modal-head">
              <h2>人工核查未知执行</h2>
              <button aria-label="关闭人工核查" onClick={() => setOpen(null)}>
                关闭
              </button>
            </div>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (String(values.note || "").trim().length < 5) {
                  setFormError("核查记录至少 5 个字符。");
                  return;
                }
                if (native && !values.confirm_container_stopped) {
                  setFormError("请先确认原生容器已停止并完成状态核查。");
                  return;
                }
                if (!native && !values.confirm_mock_runner_stopped) {
                  setFormError("请先确认模拟 Runner 已停止对应脚本。");
                  return;
                }
                await act(async () => {
                  await api(`/executions/${executionId}/reconcile`, "POST", {
                    note: String(values.note).trim(),
                    confirm_container_stopped: !!values.confirm_container_stopped,
                    confirm_mock_runner_stopped:
                      !!values.confirm_mock_runner_stopped,
                  });
                  setOpen(null);
                });
              }}
            >
              <div className="hint">
                平台会向执行控制器确认容器状态；容器仍在运行时禁止解除锁。核查完成后本周期记录保留，不自动重放。
              </div>
              {formError && <div className="error">{formError}</div>}
              {native ? (
                <label className="checkbox">
                  <input
                    type="checkbox"
                    aria-label="确认容器已停止"
                    checked={!!values.confirm_container_stopped}
                    onChange={(e) =>
                      setValues((v) => ({
                        ...v,
                        confirm_container_stopped: e.target.checked,
                      }))
                    }
                  />
                  已确认本次原生容器已停止，并核对过容器与状态文件
                </label>
              ) : (
                <label className="checkbox">
                  <input
                    type="checkbox"
                    aria-label="确认模拟 Runner 已停止"
                    checked={!!values.confirm_mock_runner_stopped}
                    onChange={(e) =>
                      setValues((v) => ({
                        ...v,
                        confirm_mock_runner_stopped: e.target.checked,
                      }))
                    }
                  />
                  已确认隔离执行进程中的脚本已停止
                </label>
              )}
              <label className="field">
                <span>核查记录（至少 5 个字符）</span>
                <textarea
                  aria-label="核查记录"
                  value={values.note || ""}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, note: e.target.value }))
                  }
                />
              </label>
              <div className="modal-footer">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setOpen(null)}
                >
                  取消
                </button>
                <button className="primary" disabled={busy}>
                  {busy ? "处理中…" : "确认核查并解除锁"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
      {open?.kind === "state-review" && detail && (
        <div className="overlay" role="dialog" aria-label="复核并合并状态候选">
          <div className="modal exec-modal">
            <div className="modal-head">
              <h2>复核并合并状态候选</h2>
              <button aria-label="关闭状态复核" onClick={() => setOpen(null)}>
                关闭
              </button>
            </div>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (!values.confirm_candidate_reviewed) {
                  setFormError("请先下载并核对状态候选文件及其摘要。");
                  return;
                }
                if (String(values.note || "").trim().length < 5) {
                  setFormError("复核记录至少 5 个字符。");
                  return;
                }
                await act(async () => {
                  await api(
                    `/executions/${executionId}/state-review/commit`,
                    "POST",
                    {
                      candidate_sha256:
                        detail.state_review_candidate_sha256,
                      confirmed: true,
                      note: String(values.note).trim(),
                    },
                  );
                  setOpen(null);
                });
              }}
            >
              <div className="hint">
                平台会基于当前最新代次重新合并，并校验本次运行的钱包白名单。候选摘要变化或状态锁不属于本次执行时，合并会被拒绝。
              </div>
              {formError && <div className="error">{formError}</div>}
              <label className="checkbox">
                <input
                  type="checkbox"
                  aria-label="确认已核对状态候选"
                  checked={!!values.confirm_candidate_reviewed}
                  onChange={(e) =>
                    setValues((v) => ({
                      ...v,
                      confirm_candidate_reviewed: e.target.checked,
                    }))
                  }
                />
                已下载并核对候选文件与 SHA-256，确认合并到最新状态
              </label>
              <label className="field">
                <span>复核记录（至少 5 个字符）</span>
                <textarea
                  aria-label="状态候选复核记录"
                  value={values.note || ""}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, note: e.target.value }))
                  }
                />
              </label>
              <div className="modal-footer">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setOpen(null)}
                >
                  取消
                </button>
                <button className="primary" disabled={busy}>
                  {busy ? "处理中…" : "确认合并状态候选"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
