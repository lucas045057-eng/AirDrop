import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Bell,
  Boxes,
  CalendarClock,
  Check,
  ChevronRight,
  FileCode2,
  FolderKanban,
  LayoutDashboard,
  LogOut,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Wallet,
  X,
} from "lucide-react";
import "./style.css";
import SmartAnalysis from "./SmartAnalysis";
type Row = Record<string, any>;
type FormField = {
  key: string;
  label: string;
  type?: string;
  options?: { value: string; label: string }[];
  required?: boolean;
  disabled?: boolean;
  note?: string;
  min?: number;
  max?: number;
};
type DialogSpec = {
  title: string;
  fields?: FormField[];
  initial?: Row;
  content?: React.ReactNode;
  submit?: (v: Row) => Promise<any>;
  button?: string;
  destructive?: boolean;
  keepOpen?: boolean;
};
let csrf = "";
async function api(path: string, method = "GET", body?: any) {
  const isForm = body instanceof FormData;
  const r = await fetch("/api" + path, {
    method,
    credentials: "same-origin",
    headers: {
      ...(method === "GET" ? {} : { "X-CSRF-Token": csrf }),
      ...(!isForm && body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
  });
  if (!r.ok) {
    let d;
    try {
      d = (await r.json()).detail;
    } catch {
      d = `请求失败 (${r.status})`;
    }
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return r.headers.get("content-type")?.includes("application/octet-stream")
    ? r.blob()
    : r.json();
}
const labels: Row = {
  SUCCESS: "成功",
  FAILED: "失败",
  PARTIAL_SUCCESS: "部分成功",
  TIMEOUT: "超时",
  CANCELLED: "已取消",
  QUEUED: "排队中",
  RUNNING: "运行中",
  SKIPPED: "已跳过",
  ALREADY_DONE: "已完成",
  UNKNOWN: "结果未知",
  MANUAL_REQUIRED: "需人工核查",
  UPLOADED: "待适配",
  REVIEW_REQUIRED: "待审核",
  APPROVED: "已审核",
  TESTED: "测试通过",
  PUBLISHED: "已发布",
  ONLINE: "在线",
  OFFLINE: "离线",
};
function Badge({ status }: { status: string }) {
  const color = [
    "SUCCESS",
    "PUBLISHED",
    "TESTED",
    "ONLINE",
    "ALREADY_DONE",
  ].includes(status)
    ? "green"
    : ["FAILED", "UNKNOWN", "TIMEOUT", "MANUAL_REQUIRED", "OFFLINE"].includes(
          status,
        )
      ? "red"
      : ["RUNNING", "QUEUED", "APPROVED"].includes(status)
        ? "blue"
        : "gray";
  return (
    <span className={"badge " + color}>
      <i />
      {labels[status] || status}
    </span>
  );
}
const fmt = (n: number) =>
  n
    ? new Date(n * 1000).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "—";
const short = (s: string) =>
  s?.length > 25 ? s.slice(0, 10) + "…" + s.slice(-8) : s;
const pretty = (o: any) => JSON.stringify(o, null, 2);
const options = (rows: Row[], label: (r: Row) => string = (r) => r.name) =>
  rows.map((r) => ({ value: r.id, label: label(r) }));
function download(blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "airdrop-wallet-backup.enc";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function Table({
  rows,
  columns,
}: {
  rows: Row[];
  columns: { title: string; render: (r: Row) => React.ReactNode }[];
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.title}>{c.title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id || i}>
              {columns.map((c) => (
                <td key={c.title}>{c.render(r)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && (
        <div className="empty">
          <Boxes size={32} />
          <strong>这里还没有记录</strong>
          <p>可以上传脚本或创建钱包，开始管理项目资料。</p>
        </div>
      )}
    </div>
  );
}
function Panel({
  title,
  extra,
  children,
}: {
  title: string;
  extra?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-title">
        <h2>{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}
const NEW_WALLET_GROUP = "__new_wallet_group__";
function WalletGroupPicker({
  groups,
  value,
  name,
  onSelect,
  onName,
}: {
  groups: Row[];
  value: string;
  name: string;
  onSelect: (value: string) => void;
  onName: (value: string) => void;
}) {
  const creating = value === NEW_WALLET_GROUP;
  const duplicate = groups.some((g) => g.name === name.trim());
  return (
    <div className="wallet-group-picker">
      <label className="field">
        <span>钱包分组</span>
        <select
          aria-label="钱包分组"
          required
          value={value}
          onChange={(e) => onSelect(e.target.value)}
        >
          <option value="" disabled>
            请选择分组
          </option>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}（{g.count} 个钱包）
            </option>
          ))}
          <option value={NEW_WALLET_GROUP}>＋ 新建分组</option>
        </select>
        <small>
          {groups.length
            ? "选择已有分组，或在这里直接新建。"
            : "暂无分组，填写名称后会与钱包一起创建。"}
        </small>
      </label>
      {creating && (
        <label className="field">
          <span>新分组名称</span>
          <input
            aria-label="新分组名称"
            required
            maxLength={100}
            placeholder="例如：项目 A 钱包"
            value={name}
            aria-invalid={duplicate || undefined}
            onChange={(e) => onName(e.target.value)}
          />
          <small className={duplicate ? "danger-text" : ""}>
            {duplicate
              ? "已有同名分组，请从列表选择。"
              : "名称最多 100 个字符，生成或导入成功后才创建分组。"}
          </small>
        </label>
      )}
    </div>
  );
}
function App() {
  const [user, setUser] = useState<Row | null>(null),
    [boot, setBoot] = useState(true),
    [page, setPage] = useState(() =>
      new URLSearchParams(location.hash.slice(1)).has("analysis")
        ? "scripts"
        : "dashboard",
    ),
    [smartVersionId, setSmartVersionId] = useState(
      () => new URLSearchParams(location.hash.slice(1)).get("analysis") || "",
    ),
    [data, setData] = useState<Row>({
      dashboard: {},
      scripts: [],
      wallets: [],
      groups: [],
      projects: [],
      tasks: [],
      executions: [],
      schedules: [],
      settings: {},
      audit: [],
    }),
    [spec, setSpec] = useState<DialogSpec | null>(null),
    [values, setValues] = useState<Row>({}),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [query, setQuery] = useState(""),
    [focused, setFocused] = useState<string | null>(null),
    [detail, setDetail] = useState<Row | null>(null),
    [groupWallets, setGroupWallets] = useState<Row[] | null>(null),
    [activeGroupId, setActiveGroupId] = useState<string | null>(null),
    [selectedWallets, setSelectedWallets] = useState<Set<string>>(new Set());
  const executionEnabled =
    data.settings.capabilities?.execution_enabled === true;
  const visibleWallets = data.wallets.filter(
    (w: Row) =>
      (!groupWallets || groupWallets.some((g) => g.id === w.id)) &&
      (!query || w.name.includes(query) || w.address.includes(query)),
  );
  const allVisibleSelected =
    visibleWallets.length > 0 &&
    visibleWallets.every((w: Row) => selectedWallets.has(w.id));
  useEffect(() => {
    setSelectedWallets(new Set());
  }, [query, activeGroupId, page, user]);
  useEffect(() => {
    setSelectedWallets((selected) => {
      const alive = new Set<string>(data.wallets.map((w: Row) => w.id));
      const next = new Set([...selected].filter((id) => alive.has(id)));
      return next.size === selected.size ? selected : next;
    });
  }, [data.wallets]);
  const put = (key: string, value: any) =>
    setValues((v) => ({ ...v, [key]: value }));
  async function load() {
    const paths: Row = {
      dashboard: "/dashboard",
      scripts: "/scripts",
      wallets: "/wallets",
      groups: "/wallets/groups",
      projects: "/projects",
      tasks: "/tasks",
      executions: "/executions",
      schedules: "/schedules",
      settings: "/settings",
      audit: "/audit",
    };
    const pairs = await Promise.all(
      Object.entries(paths).map(async ([k, p]) => [k, await api(p)]),
    );
    const next = Object.fromEntries(pairs);
    if (activeGroupId) {
      if (next.groups.some((g: Row) => g.id === activeGroupId)) {
        setGroupWallets(await api("/wallets?group_id=" + activeGroupId));
      } else {
        clearGroupFilter();
      }
    }
    setData(next);
  }
  useEffect(() => {
    api("/auth/me")
      .then((v) => {
        csrf = v.csrf;
        setUser(v);
      })
      .catch(() => {})
      .finally(() => setBoot(false));
  }, []);
  useEffect(() => {
    if (!user) return;
    load().catch((e) => setError(e.message));
    const timer = setInterval(() => {
      Promise.all([
        api("/dashboard"),
        api("/executions"),
        api("/scripts"),
        api("/settings"),
      ])
        .then(([dashboard, executions, scripts, settings]) =>
          setData((d) => ({ ...d, dashboard, executions, scripts, settings })),
        )
        .catch(() => {});
    }, 4000);
    return () => clearInterval(timer);
  }, [user]);
  useEffect(() => {
    if (!focused || page !== "executions") return;
    const update = () =>
      api("/executions/" + focused)
        .then(setDetail)
        .catch((e) => setError(e.message));
    update();
    const timer = setInterval(update, 2000);
    return () => clearInterval(timer);
  }, [focused, page]);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(""), 7000);
    return () => clearTimeout(t);
  }, [notice]);
  function open(s: DialogSpec) {
    setSpec(s);
    setValues(s.initial || {});
    setError("");
  }
  async function act(
    fn: () => Promise<any>,
    message = "操作完成",
    refresh = true,
  ) {
    setBusy(true);
    setError("");
    try {
      const result = await fn();
      setNotice(result?.warning || message);
      if (user && refresh) await load();
      return result;
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const current = spec;
    if (!current?.submit) return;
    const r = await act(() => current.submit!(values));
    if (r && !current.keepOpen) setSpec(null);
  }
  function deleteRecord(
    path: string,
    name: string,
    note: string,
    after?: () => void,
  ) {
    open({
      title: "永久删除",
      destructive: true,
      content: (
        <div className="hint">
          <strong>{name}</strong>
          <p>{note}</p>
          <p>
            此操作无法撤销。存在任务、项目或历史引用时会阻止删除并提示关联。
          </p>
        </div>
      ),
      button: "确认永久删除",
      submit: async () => {
        const result = await api(path, "DELETE", { confirmed: true });
        after?.();
        return result;
      },
    });
  }
  function deleteSelectedWallets() {
    const selected = data.wallets.filter((w: Row) => selectedWallets.has(w.id));
    if (!selected.length) return;
    open({
      title: "批量永久删除钱包",
      destructive: true,
      keepOpen: true,
      button: `确认删除选中的 ${selected.length} 个钱包`,
      content: (
        <>
          <div className="hint">
            将删除未被引用的钱包及其加密私钥和分组关系。有关联的钱包会保留并单独列出原因。此操作无法撤销，需要保留私钥时请先备份。
          </div>
          <div className="batch-wallet-preview">
            <Table
              rows={selected}
              columns={[
                { title: "名称", render: (w) => w.name || "未命名" },
                {
                  title: "地址",
                  render: (w) => (
                    <code title={w.address}>{short(w.address)}</code>
                  ),
                },
              ]}
            />
          </div>
        </>
      ),
      submit: async () => {
        const result = await api("/wallets/bulk-delete", "POST", {
          confirmed: true,
          wallet_ids: selected.map((w: Row) => w.id),
        });
        setSelectedWallets(new Set(result.blocked.map((w: Row) => w.id)));
        open({
          title: "批量删除结果",
          content: (
            <>
              <div className="hint" role="status">
                已删除 {result.deleted_count} 个钱包；{result.blocked.length}{" "}
                个因关联保留；{result.missing_ids.length} 个已不存在。
              </div>
              {result.blocked.length > 0 && (
                <>
                  <p className="muted">
                    以下钱包未删除。请先解除项目绑定或清理相关执行历史，再重新勾选删除。
                  </p>
                  <Table
                    rows={result.blocked}
                    columns={[
                      { title: "名称", render: (w) => w.name || "未命名" },
                      {
                        title: "地址",
                        render: (w) => (
                          <code title={w.address}>{short(w.address)}</code>
                        ),
                      },
                      { title: "保留原因", render: (w) => w.reason },
                    ]}
                  />
                </>
              )}
            </>
          ),
        });
        return result;
      },
    });
  }
  function decryptWalletBackup() {
    open({
      title: "解密钱包备份",
      content: (
        <iframe
          className="wallet-decrypt-frame"
          src="/wallet-backup-decrypt.html"
          title="钱包备份解密"
          sandbox="allow-scripts allow-same-origin allow-downloads allow-forms"
        />
      ),
    });
  }
  const field = (
    key: string,
    label: string,
    type = "text",
    extra: Partial<FormField> = {},
  ): FormField => ({ key, label, type, ...extra });
  const select = (
    key: string,
    label: string,
    opts: { value: string; label: string }[],
  ) => field(key, label, "select", { options: opts, required: true });
  const chainOptions = [
    { value: "EVM", label: "EVM" },
    { value: "SOLANA", label: "Solana" },
  ];
  function clearGroupFilter() {
    setActiveGroupId(null);
    setGroupWallets(null);
  }
  function groupInitial() {
    return {
      group_id: activeGroupId || data.groups[0]?.id || NEW_WALLET_GROUP,
      group_name: "",
    };
  }
  function groupDestination(v: Row) {
    if (v.group_id !== NEW_WALLET_GROUP) {
      if (!data.groups.some((g: Row) => g.id === v.group_id)) {
        throw new Error("请选择钱包分组；如果分组已删除，请刷新后重试。");
      }
      return { group_id: v.group_id };
    }
    const name = String(v.group_name || "").trim();
    if (!name || name.length > 100)
      throw new Error("请输入 1–100 个字符的分组名称。");
    if (data.groups.some((g: Row) => g.name === name)) {
      throw new Error("已有同名分组，请从列表选择。");
    }
    return { group_name: name };
  }
  function upload() {
    open({
      title: "上传脚本包",
      content: (
        <div className="hint">
          支持 .py、.zip、.tar.gz，最大 10
          MiB。上传与静态分析不会运行代码，原始包加密保存。
        </div>
      ),
      fields: [
        field("file", "选择文件", "file", {
          required: true,
          note: "请移除 .env、.git、符号链接与硬编码凭证。",
        }),
      ],
      submit: (v) => {
        const body = new FormData();
        body.append("file", v.file);
        return api("/scripts/upload", "POST", body);
      },
    });
  }
  function generate() {
    open({
      title: "批量生成钱包",
      initial: { chain: "EVM", count: 10, ...groupInitial() },
      fields: [
        select("chain", "链类型", chainOptions),
        field("count", "数量（1–1000）", "number", {
          required: true,
          min: 1,
          max: 1000,
        }),
        field("group_id", "钱包分组", "wallet-group"),
      ],
      content: (
        <div className="hint">
          每个钱包独立随机生成。创建后请下载加密备份并执行恢复验证。
        </div>
      ),
      button: "生成钱包",
      submit: async (v) => {
        const destination = groupDestination(v);
        const result = await api("/wallets/generate", "POST", {
          chain: v.chain,
          count: Number(v.count),
          ...destination,
        });
        const name =
          destination.group_name ||
          data.groups.find((g: Row) => g.id === destination.group_id)?.name;
        return {
          ...result,
          warning: `已生成 ${result.created} 个钱包，加入分组“${name}”。请下载加密备份。`,
        };
      },
    });
  }
  function backup() {
    open({
      title: "下载加密钱包备份",
      fields: [
        field("password", "备份密码（至少 12 字符）", "password", {
          required: true,
          note: "请单独保管密码，备份可在其他安装中恢复。",
        }),
      ],
      button: "下载加密备份",
      submit: async (v) => {
        download(await api("/wallets/backup", "POST", v));
        return true;
      },
    });
  }
  function task(project?: Row) {
    open({
      title: "配置执行任务",
      initial: {
        project_id: project?.id || data.projects[0]?.id || "",
        script_version_id: data.scripts.find((s: Row) => s.manifest)?.id || "",
        name: "每日签到",
        batch_size: 100,
        max_retries: 0,
      },
      fields: [
        select("project_id", "项目", options(data.projects)),
        select(
          "script_version_id",
          "已适配脚本版本",
          options(
            data.scripts.filter((s: Row) => s.manifest),
            (s) => `${s.name} v${s.version} · ${labels[s.status]}`,
          ),
        ),
        field("name", "任务名称", "text", { required: true }),
        field("batch_size", "每批钱包数量", "number"),
        field("max_retries", "安全自动重试次数（0–3）", "number"),
      ],
      submit: (v) =>
        api("/tasks", "POST", {
          ...v,
          batch_size: Number(v.batch_size),
          max_retries: Number(v.max_retries),
        }),
    });
  }
  function adapt(s: Row) {
    const formatOpts = ["CSV", "TXT", "XLSX", "JSON"].map((value) => ({
      value,
      label: value,
    }));
    open({
      title: "标准化脚本适配",
      initial: {
        entrypoint: s.analysis.entrypoints[0]?.path || "",
        chain: "EVM",
        format: "CSV",
        input_mode: "file",
        path: "inputs/wallets.csv",
        fields: pretty({ address: "address", name: "name" }),
        mappings: pretty(
          Object.fromEntries(
            s.analysis.wallet_paths
              .filter((p: Row) => p.absolute)
              .map((p: Row) => [p.path, "inputs/wallets.csv"]),
          ),
        ),
        arguments: "[]",
        environment: "AIRDROP_WALLET_PATH",
        timeout: 60,
        mode: "batch",
        steps: "[]",
        retry: false,
        risk: false,
        provider: "rules",
        reviewed_preview_sha256: "",
      },
      fields: [
        field("entrypoint", "执行入口（须明确确认）", "text", {
          required: true,
          note: s.analysis.entrypoints.map((p: Row) => p.path).join(" / "),
        }),
        select("chain", "钱包链", chainOptions),
        select("format", "输入文件格式", formatOpts),
        select("input_mode", "输入模式", [
          { value: "file", label: "文件" },
          { value: "environment", label: "环境变量" },
          { value: "cli", label: "命令行参数" },
        ]),
        field("path", "安全相对钱包路径"),
        field("fields", "字段映射（脚本列名 → 标准字段）", "textarea", {
          note: "TXT 一行一个字段，须只映射一个字段。account 等歧义字段不可自动猜测。",
        }),
        field("mappings", "旧钱包路径 → 新路径（JSON）", "textarea"),
        field(
          "arguments",
          "命令行参数（JSON 数组，可使用 {wallet_path}）",
          "textarea",
        ),
        field("environment", "环境变量名称"),
        field("timeout", "执行超时秒数（1–300）", "number"),
        select("mode", "执行模式", [
          { value: "batch", label: "批量" },
          { value: "single", label: "单钱包" },
          { value: "workflow", label: "多步骤工作流" },
        ]),
        field("steps", "工作流入口（JSON 数组）", "textarea"),
        select("provider", "适配 Provider", [
          { value: "rules", label: "规则式适配" },
          { value: "compatible", label: "外部兼容 API" },
        ]),
        field(
          "reviewed_preview_sha256",
          "已审核外部 AI 预览 SHA-256（规则适配留空）",
          "text",
        ),
        field("retry", "确认任务可安全重试", "checkbox"),
        field("risk", "允许占位字段兼容（不传递真实私钥）", "checkbox"),
      ],
      content: (
        <div className="hint">
          源代码与 Manifest 将创建为新版本。外部 AI
          须先查看并审核脱敏预览，再填写 SHA-256；含真实凭证的源代码禁止执行。
        </div>
      ),
      submit: (v) =>
        api(`/scripts/${s.id}/adapt`, "POST", {
          provider: v.provider,
          reviewed_preview_sha256: v.reviewed_preview_sha256 || null,
          path_mappings: JSON.parse(v.mappings),
          manifest: {
            package_id: s.package_id,
            package_version: s.version,
            entrypoint: v.entrypoint,
            wallet_chain: v.chain,
            wallet_input_mode: v.input_mode,
            wallet_format: v.format,
            wallet_path: v.path,
            wallet_field_mapping: JSON.parse(v.fields),
            arguments: JSON.parse(v.arguments),
            environment_name: v.environment,
            timeout_seconds: Number(v.timeout),
            execution_mode: v.mode,
            steps: JSON.parse(v.steps),
            retry_safe: v.retry,
            high_risk_compatibility: v.risk,
            dependencies: v.format === "XLSX" ? ["openpyxl"] : [],
          },
        }),
    });
  }
  async function scriptReview(s: Row) {
    await act(async () => {
      const diff = await api(`/scripts/${s.id}/diff`);
      open({
        title: "审核 Diff 与 Manifest",
        content: (
          <>
            <pre>{diff.diff || "无代码修改，仅更新运行配置。"}</pre>
            <pre>{pretty(s.manifest)}</pre>
          </>
        ),
        initial: { confirmed: false },
        fields: [
          field(
            "confirmed",
            "我已审阅修改内容、钱包映射与权限，批准保存此版本",
            "checkbox",
            { required: true },
          ),
        ],
        submit: () => api(`/scripts/${s.id}/approve`, "POST"),
      });
      return true;
    }, "请审阅版本修改");
  }
  function show(title: string, content: any) {
    open({
      title,
      content: (
        <pre>{typeof content === "string" ? content : pretty(content)}</pre>
      ),
    });
  }
  const nav = [
    ["dashboard", "总览", LayoutDashboard],
    ["scripts", "脚本中心", FileCode2],
    ["wallets", "钱包中心", Wallet],
    ["projects", "项目中心", FolderKanban],
    ["executions", "执行中心", Activity],
    ["settings", "系统设置", Settings2],
  ] as const;
  const title = nav.find((n) => n[0] === page)?.[1] || "总览";
  const run = (t: Row, test = false) =>
    act(async () => {
      const r = await api(`/tasks/${t.id}/${test ? "test" : "run"}`, "POST");
      setPage("executions");
      setFocused(r.execution_id);
      return r;
    }, "已提交独立 Worker 队列");
  const executionColumns = [
    {
      title: "任务 / 项目",
      render: (e: Row) => (
        <>
          <button
            className="name-button"
            onClick={() => {
              setPage("executions");
              setFocused(e.id);
            }}
          >
            {e.task_name}
          </button>
          <small>
            {e.project_name} · {e.is_test ? "隔离测试" : "任务历史"}
          </small>
        </>
      ),
    },
    {
      title: "钱包结果",
      render: (e: Row) => (
        <>
          <span className="success-text">{e.counts.SUCCESS || 0}</span> /{" "}
          {e.wallet_count}
          <small>
            失败 {e.counts.FAILED || 0} · 跳过 {e.counts.SKIPPED || 0}
          </small>
        </>
      ),
    },
    { title: "状态", render: (e: Row) => <Badge status={e.status} /> },
    { title: "创建时间", render: (e: Row) => fmt(e.created_at) },
  ];
  function stats() {
    return (
      <div className="stats">
        {[
          ["项目总数", data.dashboard.projects, FolderKanban, "blue"],
          ["钱包总数", data.dashboard.wallets, Wallet, "purple"],
          ["今日任务", data.dashboard.today_tasks, Activity, "cyan"],
          ["今日成功", data.dashboard.today_success, ShieldCheck, "green"],
        ].map(([label, value, Icon, color], i) => {
          const I = Icon as React.ElementType;
          return (
            <div className="stat" key={i}>
              <div>
                <span>{String(label)}</span>
                <strong>
                  {Number(value) || 0}
                  <small>项</small>
                </strong>
              </div>
              <div className={"stat-icon " + color}>
                <I size={22} />
              </div>
            </div>
          );
        })}
      </div>
    );
  }
  if (boot) return <div className="loading">正在连接 AirDrop V2…</div>;
  if (!user)
    return (
      <div className="login">
        <div className="login-brand">
          <div className="logo big">
            <Boxes size={30} />
          </div>
          <h1>
            AirDrop <b>V2</b>
          </h1>
          <p>让每一个钱包任务，都有迹可循。</p>
          <div className="login-flow">
            脚本接入
            <ArrowRight size={15} />
            审核与测试
            <ArrowRight size={15} />
            自动执行
          </div>
        </div>
        <div className="login-card">
          <span className="eyebrow">AUTOMATION WORKSPACE</span>
          <h2>欢迎回来</h2>
          <p>登录你的自动化控制台</p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              act(async () => {
                const v = await api("/auth/login", "POST", {
                  username: values.username ?? "",
                  password: values.password,
                });
                csrf = v.csrf;
                setUser(v);
                setValues({});
                return v;
              }, "登录成功");
            }}
          >
            <label className="field">
              <span>管理员账号</span>
              <input
                required
                autoComplete="username"
                placeholder="请输入管理员账号"
                value={values.username ?? ""}
                onChange={(e) => put("username", e.target.value)}
              />
            </label>
            <label className="field">
              <span>登录密码</span>
              <input
                required
                type="password"
                autoComplete="current-password"
                value={values.password || ""}
                onChange={(e) => put("password", e.target.value)}
              />
            </label>
            {error && <div className="error">{error}</div>}
            <button className="primary full" disabled={busy}>
              {busy ? "正在登录…" : "登录控制台"}
              <ArrowRight size={17} />
            </button>
          </form>
          <div className="login-safe">
            <ShieldCheck size={16} />
            当前为管理模式，脚本执行与链上交易尚未开放
          </div>
          <small>初始密码保存在项目 secrets/dev-login.txt</small>
        </div>
      </div>
    );
  return (
    <div className="app">
      <aside className="sidebar">
        <a className="brand" onClick={() => setPage("dashboard")}>
          <div className="logo">
            <Boxes size={23} />
          </div>
          <strong>
            AirDrop <span>V2</span>
          </strong>
        </a>
        <div className="workspace-label">
          工作空间<span>个人版</span>
        </div>
        <nav>
          {nav.map(([id, label, Icon]) => (
            <button
              key={id}
              className={page === id ? "active" : ""}
              onClick={() => {
                setPage(id);
                setFocused(null);
                setDetail(null);
              }}
            >
              <Icon size={19} />
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="safe-card">
            <ShieldCheck size={21} />
            <strong>管理模式</strong>
            <p>管理钱包与脚本 · 执行未开放</p>
          </div>
          <div className="profile">
            <span className="avatar">A</span>
            <div>
              <strong>{user.username}</strong>
              <small>管理员</small>
            </div>
            <button
              aria-label="退出登录"
              onClick={() =>
                act(async () => {
                  await api("/auth/logout", "POST");
                  setUser(null);
                  csrf = "";
                  return true;
                })
              }
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header>
          <div>
            工作空间
            <ChevronRight size={14} />
            <strong>{title}</strong>
          </div>
          <div className="header-right">
            <span className="env-dot">管理模式 · 执行未开放</span>
            <button aria-label="执行告警" onClick={() => setPage("executions")}>
              <Bell size={18} />
            </button>
            <span className="avatar small">A</span>
          </div>
        </header>
        <main>
          <div className="page-head">
            <div>
              <div className="eyebrow">
                {page === "dashboard"
                  ? "WORKSPACE OVERVIEW"
                  : "AUTOMATION WORKSPACE"}
              </div>
              <h1>{page === "dashboard" ? "自动化工作台" : title}</h1>
              <p>
                {
                  (
                    {
                      dashboard:
                        "管理脚本、连接钱包，查看每一次任务的执行结果。",
                      scripts: "上传、分析、适配与审核，统一管理脚本版本。",
                      wallets: "独立管理钱包、分组与加密备份。",
                      projects: "组织脚本与钱包，为每个项目配置执行周期。",
                      executions: "查看钱包结果、执行日志、重试与调度历史。",
                      settings: "管理 AI、Worker、执行策略与安全状态。",
                    } as Row
                  )[page]
                }
              </p>
            </div>
            <div className="actions">
              <button
                className="secondary"
                onClick={() => act(load, "数据已刷新")}
              >
                <RefreshCw size={15} />
                刷新
              </button>
              {page === "scripts" && (
                <button className="primary" onClick={upload}>
                  <UploadCloud size={15} />
                  上传脚本
                </button>
              )}
              {page === "wallets" && (
                <button className="primary" onClick={generate}>
                  <Plus size={15} />
                  生成钱包
                </button>
              )}
              {page === "projects" && (
                <button
                  className="primary"
                  onClick={() =>
                    open({
                      title: "新建项目",
                      initial: {
                        timezone: "Asia/Shanghai",
                        reset_hour: 0,
                        concurrency: 1,
                      },
                      fields: [
                        field("name", "项目名称", "text", { required: true }),
                        field("description", "描述", "textarea"),
                        field("timezone", "业务时区"),
                        field("reset_hour", "周期重置小时（0–23）", "number"),
                        field("concurrency", "项目并发上限", "number"),
                      ],
                      submit: (v) =>
                        api("/projects", "POST", {
                          ...v,
                          reset_hour: Number(v.reset_hour),
                          concurrency: Number(v.concurrency),
                        }),
                    })
                  }
                >
                  <Plus size={15} />
                  新建项目
                </button>
              )}
              {page === "executions" && (
                <button
                  className="primary"
                  onClick={() =>
                    open({
                      title: "新建自动执行计划",
                      initial: {
                        task_id: data.tasks[0]?.id || "",
                        timezone: "Asia/Shanghai",
                        time: "09:00",
                        weekdays: "[0,1,2,3,4,5,6]",
                        enabled: false,
                        misfire_policy: "skip",
                      },
                      fields: [
                        select("task_id", "任务", options(data.tasks)),
                        field("time", "每天执行时间", "time"),
                        field("timezone", "时区"),
                        field(
                          "weekdays",
                          "星期（0=周一，6=周日，JSON 数组）",
                          "textarea",
                        ),
                        select("misfire_policy", "错过计划处理", [
                          { value: "skip", label: "超出 5 分钟跳过" },
                          { value: "catch_up_once", label: "补执行一次" },
                        ]),
                        field(
                          "enabled",
                          "主动启用自动执行（默认关闭）",
                          "checkbox",
                          {
                            disabled: !executionEnabled,
                            note: "管理模式仅允许保存停用计划。",
                          },
                        ),
                      ],
                      submit: (v) =>
                        api("/schedules", "POST", {
                          task_id: v.task_id,
                          timezone: v.timezone,
                          hour: Number(v.time.split(":")[0]),
                          minute: Number(v.time.split(":")[1]),
                          weekdays: JSON.parse(v.weekdays),
                          enabled: v.enabled,
                          misfire_policy: v.misfire_policy,
                        }),
                    })
                  }
                >
                  <CalendarClock size={15} />
                  新建计划
                </button>
              )}
            </div>
          </div>
          {error && (
            <div className="error banner">
              {error}
              <button onClick={() => setError("")} aria-label="关闭错误">
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="toast">
              <Check size={16} />
              {notice}
            </div>
          )}
          {page === "dashboard" && (
            <>
              {stats()}
              <div className="overview-grid">
                <Panel
                  title="最近执行"
                  extra={
                    <button
                      className="text-button"
                      onClick={() => setPage("executions")}
                    >
                      查看全部
                      <ArrowRight size={14} />
                    </button>
                  }
                >
                  <Table
                    rows={data.executions.slice(0, 7)}
                    columns={executionColumns}
                  />
                </Panel>
                <div>
                  <Panel title="运行概况">
                    <div className="status-line">
                      <span>运行中任务</span>
                      <strong>{data.dashboard.running || 0}</strong>
                    </div>
                    <div className="status-line">
                      <span>今日异常任务</span>
                      <strong className="danger-text">
                        {data.dashboard.today_failed || 0}
                      </strong>
                    </div>
                    <div className="status-line">
                      <span>脚本包数量</span>
                      <strong>{data.dashboard.scripts || 0}</strong>
                    </div>
                    <div className="status-line">
                      <span>在线 Worker</span>
                      <strong>
                        {data.settings.workers?.filter(
                          (w: Row) => w.status === "ONLINE",
                        ).length || 0}
                      </strong>
                    </div>
                    <p className="worker-note">
                      持久化队列 · 独立 Worker · 逐钱包结果
                    </p>
                  </Panel>
                  <section className="guide-card">
                    <FileCode2 size={24} />
                    <h3>开始管理项目资料</h3>
                    <p>
                      上传 Python
                      或完整项目，确认入口与钱包格式，然后审核并保存。当前不执行脚本。
                    </p>
                    <button
                      onClick={() => {
                        setPage("scripts");
                        upload();
                      }}
                    >
                      接入脚本
                      <ArrowRight size={14} />
                    </button>
                  </section>
                </div>
              </div>
              <Panel
                title="资料管理流程"
                extra={<span className="muted">每个版本独立审核与验证</span>}
              >
                <div className="pipeline">
                  {[
                    "上传脚本",
                    "静态分析",
                    "规则 / AI 适配",
                    "钱包绑定",
                    "审核与保存",
                    "加密备份",
                  ].map((s, i) => (
                    <React.Fragment key={s}>
                      <span>
                        <i>{i + 1}</i>
                        {s}
                      </span>
                      {i < 5 && <ChevronRight size={15} />}
                    </React.Fragment>
                  ))}
                </div>
              </Panel>
            </>
          )}
          {page === "scripts" &&
            smartVersionId &&
            data.scripts.some((s: Row) => s.id === smartVersionId) && (
              <SmartAnalysis
                key={smartVersionId}
                version={data.scripts.find((s: Row) => s.id === smartVersionId)}
                api={api}
                projects={data.projects}
                refresh={load}
                back={() => {
                  setSmartVersionId("");
                  location.hash = "";
                }}
                settings={() => setPage("settings")}
              />
            )}
          {page === "scripts" && !smartVersionId && (
            <Panel
              title={`脚本版本 · ${data.scripts.length}`}
              extra={<span className="muted">.py · .zip · .tar.gz</span>}
            >
              <Table
                rows={data.scripts}
                columns={[
                  {
                    title: "脚本名称",
                    render: (s) => (
                      <>
                        <button
                          className="name-button"
                          onClick={() =>
                            show(s.name + " · 结构分析", s.analysis)
                          }
                        >
                          <FileCode2 size={18} />
                          {s.name}
                        </button>
                        <small>{s.sha256.slice(0, 16)}…</small>
                      </>
                    ),
                  },
                  {
                    title: "结构 / 入口",
                    render: (s) => (
                      <>
                        {s.analysis.python_count} 个 Python 文件
                        <small>
                          {s.manifest?.entrypoint ||
                            s.analysis.entrypoints
                              .map((p: Row) => p.path)
                              .join(" / ") ||
                            "需手动确认"}
                        </small>
                      </>
                    ),
                  },
                  { title: "版本", render: (s) => "v" + s.version },
                  { title: "状态", render: (s) => <Badge status={s.status} /> },
                  {
                    title: "操作",
                    render: (s) => (
                      <div className="actions">
                        <button
                          onClick={() => {
                            setSmartVersionId(s.id);
                            location.hash = new URLSearchParams({
                              analysis: s.id,
                            }).toString();
                          }}
                        >
                          <Sparkles size={13} />
                          智能分析
                        </button>
                        <button onClick={() => adapt(s)}>
                          <Sparkles size={13} />
                          适配
                        </button>
                        <button
                          onClick={() =>
                            act(async () => {
                              const p = await api(
                                `/scripts/${s.id}/ai-preview`,
                              );
                              show("脱敏预览 · SHA-256: " + p.sha256, p);
                              return true;
                            })
                          }
                        >
                          AI 预览
                        </button>
                        <button
                          onClick={() =>
                            act(async () => {
                              show(
                                "脱敏源码",
                                await api(`/scripts/${s.id}/source`),
                              );
                              return true;
                            })
                          }
                        >
                          源码
                        </button>
                        {s.manifest && (
                          <>
                            <button onClick={() => scriptReview(s)}>
                              审核 Diff
                            </button>
                            <button disabled title="当前管理模式未开放执行">
                              隔离测试
                            </button>
                            <button
                              disabled={!executionEnabled}
                              title="发布需执行验证，当前尚未开放"
                              onClick={() =>
                                act(
                                  () => api(`/scripts/${s.id}/publish`, "POST"),
                                  "版本已发布",
                                )
                              }
                            >
                              发布
                            </button>
                          </>
                        )}
                        <button
                          className="danger-button"
                          onClick={() =>
                            deleteRecord(
                              `/scripts/${s.id}`,
                              `${s.name} · v${s.version}`,
                              "删除此版本及其适配信息，清理不再使用的加密源码；最后一个版本删除后移除脚本包。",
                            )
                          }
                        >
                          删除版本
                        </button>
                      </div>
                    ),
                  },
                ]}
              />
            </Panel>
          )}
          {page === "wallets" && (
            <>
              <div className="wallet-toolbar">
                <div className="search">
                  <Search size={16} />
                  <input
                    aria-label="搜索钱包"
                    placeholder="搜索名称或钱包地址"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </div>
                <div className="actions">
                  <button
                    className="secondary"
                    onClick={() =>
                      open({
                        title: "导入钱包文件",
                        initial: {
                          chain: "EVM",
                          ...groupInitial(),
                          mapping:
                            '{"private_key":"private_key","address":"address"}',
                        },
                        fields: [
                          select("chain", "链类型", chainOptions),
                          field("group_id", "钱包分组", "wallet-group"),
                          field(
                            "file",
                            "TXT / CSV / XLSX / JSON 文件",
                            "file",
                            { required: true },
                          ),
                          field(
                            "mapping",
                            "字段映射（文件字段 → 标准字段）",
                            "textarea",
                          ),
                        ],
                        content: (
                          <div className="hint">
                            导入钱包加密保存，当前版本禁止执行导入的钱包。不会猜测歧义字段。
                          </div>
                        ),
                        submit: (v) => {
                          const f = new FormData();
                          f.append("file", v.file);
                          f.append("chain", v.chain);
                          const destination = groupDestination(v);
                          Object.entries(destination).forEach(([key, value]) =>
                            f.append(key, value),
                          );
                          f.append("mapping_json", v.mapping);
                          return api("/wallets/import-file", "POST", f);
                        },
                      })
                    }
                  >
                    <UploadCloud size={14} />
                    导入
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      open({
                        title: "创建钱包分组",
                        initial: { wallet_ids: [] },
                        fields: [
                          field("name", "分组名称", "text", { required: true }),
                          {
                            key: "wallet_ids",
                            label: "选择钱包",
                            type: "multi",
                            options: options(
                              data.wallets,
                              (w) => `${w.name} · ${short(w.address)}`,
                            ),
                          },
                        ],
                        submit: (v) => api("/wallets/groups", "POST", v),
                      })
                    }
                  >
                    <Plus size={14} />
                    分组
                  </button>
                  <button className="secondary" onClick={backup}>
                    <ArrowDownToLine size={14} />
                    加密备份
                  </button>
                  <button className="secondary" onClick={decryptWalletBackup}>
                    解密备份
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      open({
                        title: "恢复与验证加密备份",
                        initial: { verify_only: true },
                        fields: [
                          field("file", "加密备份文件", "file", {
                            required: true,
                          }),
                          field("password", "备份密码", "password", {
                            required: true,
                          }),
                          field(
                            "verify_only",
                            "只验证恢复，不导入缺少的钱包",
                            "checkbox",
                          ),
                        ],
                        submit: (v) => {
                          const f = new FormData();
                          f.append("file", v.file);
                          f.append("password", v.password);
                          f.append("verify_only", String(v.verify_only));
                          return api("/wallets/restore", "POST", f);
                        },
                      })
                    }
                  >
                    恢复验证
                  </button>
                </div>
              </div>
              <div className="group-grid">
                <button
                  className={
                    "group-card" + (!activeGroupId ? " group-active" : "")
                  }
                  aria-pressed={!activeGroupId}
                  onClick={clearGroupFilter}
                >
                  <Wallet size={18} />
                  <strong>全部钱包</strong>
                </button>
                {data.groups.map((g: Row) => (
                  <div
                    className={
                      "group-card group-with-actions" +
                      (activeGroupId === g.id ? " group-active" : "")
                    }
                    key={g.id}
                  >
                    <button
                      className="group-select"
                      aria-pressed={activeGroupId === g.id}
                      onClick={() =>
                        act(
                          async () => {
                            setGroupWallets(
                              await api("/wallets?group_id=" + g.id),
                            );
                            setActiveGroupId(g.id);
                            return true;
                          },
                          g.name,
                          false,
                        )
                      }
                    >
                      <Wallet size={18} />
                      <div>
                        <strong>{g.name}</strong>
                        <small>{g.count} 个钱包</small>
                      </div>
                    </button>
                    <button
                      className="danger-button"
                      aria-label={"删除分组 " + g.name}
                      onClick={() =>
                        deleteRecord(
                          `/wallets/groups/${g.id}`,
                          g.name,
                          "仅删除分组与成员关系，分组中的钱包继续保留。",
                          clearGroupFilter,
                        )
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
              <Panel
                title="钱包列表"
                extra={<span className="muted">仅公开地址与元数据</span>}
              >
                <div className="wallet-bulk-toolbar">
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      aria-label="全选当前列表钱包"
                      checked={allVisibleSelected}
                      ref={(input) => {
                        if (input)
                          input.indeterminate =
                            !allVisibleSelected &&
                            visibleWallets.some((w: Row) =>
                              selectedWallets.has(w.id),
                            );
                      }}
                      onChange={(event) =>
                        setSelectedWallets(
                          new Set(
                            event.target.checked
                              ? visibleWallets.map((w: Row) => w.id)
                              : [],
                          ),
                        )
                      }
                    />
                    全选当前列表（{visibleWallets.length}）
                  </label>
                  <span className="muted" aria-live="polite">
                    已选择 {selectedWallets.size} 个钱包
                  </span>
                  <div className="actions">
                    <button
                      className="danger-button"
                      disabled={!selectedWallets.size || busy}
                      onClick={deleteSelectedWallets}
                    >
                      批量删除（{selectedWallets.size}）
                    </button>
                    <button
                      className="secondary"
                      disabled={!selectedWallets.size || busy}
                      onClick={() => setSelectedWallets(new Set())}
                    >
                      取消选择
                    </button>
                  </div>
                </div>
                <Table
                  rows={visibleWallets}
                  columns={[
                    {
                      title: "选择",
                      render: (w) => (
                        <input
                          type="checkbox"
                          aria-label={`选择钱包 ${w.name || "未命名"} ${w.address}`}
                          checked={selectedWallets.has(w.id)}
                          onChange={(event) =>
                            setSelectedWallets((previous) => {
                              const next = new Set(previous);
                              if (event.target.checked) next.add(w.id);
                              else next.delete(w.id);
                              return next;
                            })
                          }
                        />
                      ),
                    },
                    {
                      title: "名称",
                      render: (w) => (
                        <>
                          {w.name || "未命名"}
                          <small>{w.tags.join(" · ")}</small>
                        </>
                      ),
                    },
                    {
                      title: "地址",
                      render: (w) => (
                        <code title={w.address}>{short(w.address)}</code>
                      ),
                    },
                    {
                      title: "链 / 用途",
                      render: (w) => (
                        <>
                          {w.chain}
                          <small>{w.is_test ? "平台生成" : "已导入"}</small>
                        </>
                      ),
                    },
                    {
                      title: "备份",
                      render: (w) => (
                        <span
                          className={
                            w.backup_verified ? "success-text" : "warning-text"
                          }
                        >
                          {w.backup_verified ? "已验证" : "待备份验证"}
                        </span>
                      ),
                    },
                    {
                      title: "状态",
                      render: (w) => (w.enabled ? "已启用" : "已停用"),
                    },
                    {
                      title: "操作",
                      render: (w) => (
                        <div className="actions">
                          <button
                            className="text-button"
                            onClick={() =>
                              open({
                                title: "编辑钱包元数据",
                                initial: {
                                  name: w.name,
                                  tags: w.tags.join(","),
                                  enabled: w.enabled,
                                },
                                fields: [
                                  field("name", "名称"),
                                  field("tags", "标签（逗号分隔）"),
                                  field("enabled", "启用此钱包", "checkbox"),
                                ],
                                submit: (v) =>
                                  api("/wallets/" + w.id, "PUT", {
                                    ...v,
                                    tags: v.tags
                                      .split(",")
                                      .map((s: string) => s.trim())
                                      .filter(Boolean),
                                  }),
                              })
                            }
                          >
                            编辑
                          </button>
                          <button
                            className="danger-button"
                            onClick={() =>
                              deleteRecord(
                                `/wallets/${w.id}`,
                                `${w.name || "未命名钱包"} · ${short(w.address)}`,
                                "删除钱包及数据库中的加密私钥和分组关系。需要保留私钥时请先完成加密备份；已有备份文件继续保留。",
                                clearGroupFilter,
                              )
                            }
                          >
                            删除钱包
                          </button>
                        </div>
                      ),
                    },
                  ]}
                />
              </Panel>
            </>
          )}
          {page === "projects" && (
            <>
              <div className="project-grid">
                {data.projects.map((p: Row) => (
                  <section className="panel project-card" key={p.id}>
                    <div className="project-top">
                      <span className="project-icon">
                        <FolderKanban size={22} />
                      </span>
                      <span className="chain">管理项目</span>
                    </div>
                    <h2>{p.name}</h2>
                    <p>{p.description || "暂无描述"}</p>
                    <div className="project-metrics">
                      <div>
                        <strong>{p.wallet_count}</strong>
                        <small>已绑定钱包</small>
                      </div>
                      <div>
                        <strong>
                          {
                            data.tasks.filter((t: Row) => t.project_id === p.id)
                              .length
                          }
                        </strong>
                        <small>执行任务</small>
                      </div>
                    </div>
                    <div className="project-config">
                      {p.timezone} · {p.reset_hour}:00 周期重置 · 并发{" "}
                      {p.concurrency}
                    </div>
                    <div className="actions">
                      <button
                        className="secondary"
                        onClick={() =>
                          open({
                            title: "绑定钱包分组或部分钱包",
                            initial: { group_ids: p.group_ids, wallet_ids: [] },
                            fields: [
                              {
                                key: "group_ids",
                                label: "钱包分组",
                                type: "multi",
                                options: options(
                                  data.groups,
                                  (g) => `${g.name} · ${g.count} 钱包`,
                                ),
                              },
                              {
                                key: "wallet_ids",
                                label: "额外选择部分钱包",
                                type: "multi",
                                options: options(
                                  data.wallets,
                                  (w) => `${w.name} · ${short(w.address)}`,
                                ),
                              },
                            ],
                            content: (
                              <div className="hint">
                                绑定保存当前成员；分组变更须重新绑定。每次任务入队会固定独立钱包快照。
                              </div>
                            ),
                            submit: (v) =>
                              api(`/projects/${p.id}/wallets`, "PUT", v),
                          })
                        }
                      >
                        绑定钱包
                      </button>
                      <button className="primary" onClick={() => task(p)}>
                        <Plus size={14} />
                        添加任务
                      </button>
                      <button
                        className="danger-button"
                        onClick={() =>
                          deleteRecord(
                            `/projects/${p.id}`,
                            p.name,
                            "删除项目及钱包绑定、交易策略配置。有关任务或执行历史须先清理；绑定的钱包继续保留。",
                          )
                        }
                      >
                        删除项目
                      </button>
                    </div>
                    <button
                      className="text-button points-link"
                      onClick={() =>
                        act(async () => {
                          show(
                            "钱包积分历史",
                            await api(`/projects/${p.id}/points`),
                          );
                          return true;
                        })
                      }
                    >
                      查看积分历史
                      <ArrowRight size={13} />
                    </button>
                  </section>
                ))}
              </div>
              <Panel title="项目任务">
                <Table
                  rows={data.tasks}
                  columns={[
                    {
                      title: "任务名称",
                      render: (t) => (
                        <>
                          <strong>{t.name}</strong>
                          {t.feature_config_snapshot && (
                            <small>
                              功能：
                              {t.feature_config_snapshot.feature_name ||
                                t.feature_config_snapshot.feature_id}
                            </small>
                          )}
                        </>
                      ),
                    },
                    {
                      title: "功能配置",
                      render: (t) =>
                        t.feature_config_snapshot ? (
                          <button
                            className="name-button"
                            onClick={() =>
                              show(
                                "功能任务 · " + t.name,
                                t.feature_config_snapshot,
                              )
                            }
                          >
                            查看入口与参数
                          </button>
                        ) : (
                          <span className="muted">原单入口任务</span>
                        ),
                    },
                    {
                      title: "项目",
                      render: (t) =>
                        data.projects.find((p: Row) => p.id === t.project_id)
                          ?.name,
                    },
                    {
                      title: "版本 / 批次",
                      render: (t) => (
                        <>
                          v
                          {
                            data.scripts.find(
                              (s: Row) => s.id === t.script_version_id,
                            )?.version
                          }
                          <small>
                            每批 {t.batch_size} 钱包 · 自动重试 {t.max_retries}{" "}
                            次
                          </small>
                        </>
                      ),
                    },
                    {
                      title: "操作",
                      render: (t) => (
                        <div className="actions">
                          <button
                            disabled={!executionEnabled}
                            title="执行尚未开放"
                            onClick={() => run(t, true)}
                          >
                            测试
                          </button>
                          <button
                            disabled={!executionEnabled}
                            title="执行尚未开放"
                            onClick={() => run(t)}
                          >
                            <Play size={13} />
                            运行
                          </button>
                          <button
                            onClick={() => {
                              const current = data.scripts.find(
                                (s: Row) => s.id === t.script_version_id,
                              );
                              open({
                                title: "切换 / 回滚已发布版本",
                                initial: {
                                  script_version_id: t.script_version_id,
                                },
                                fields: [
                                  select(
                                    "script_version_id",
                                    "同一脚本包的已发布版本",
                                    options(
                                      data.scripts.filter(
                                        (s: Row) =>
                                          s.status === "PUBLISHED" &&
                                          s.package_id === current?.package_id,
                                      ),
                                      (s) => `${s.name} v${s.version}`,
                                    ),
                                  ),
                                ],
                                submit: (v) =>
                                  api(`/tasks/${t.id}/version`, "PUT", v),
                              });
                            }}
                          >
                            切换版本
                          </button>
                          <button
                            className="danger-button"
                            onClick={() =>
                              deleteRecord(
                                `/tasks/${t.id}`,
                                t.name,
                                "删除任务及其独占工作流。须先删除定时计划和执行记录，钱包、脚本和项目继续保留。",
                              )
                            }
                          >
                            删除任务
                          </button>
                        </div>
                      ),
                    },
                  ]}
                />
              </Panel>
            </>
          )}
          {page === "executions" && (
            <>
              {stats()}
              <Panel
                title="执行记录"
                extra={<span className="muted">每 4 秒自动刷新</span>}
              >
                <Table
                  rows={data.executions}
                  columns={[
                    ...executionColumns,
                    {
                      title: "周期 / 重试",
                      render: (e) => (
                        <>
                          {e.period.startsWith("test:")
                            ? "独立测试周期"
                            : e.period}
                          <small>重试 {e.retry_count} 次</small>
                        </>
                      ),
                    },
                    {
                      title: "操作",
                      render: (e) => (
                        <div className="actions">
                          <button onClick={() => setFocused(e.id)}>明细</button>
                          <button
                            className="danger-button"
                            disabled={
                              ![
                                "SUCCESS",
                                "PARTIAL_SUCCESS",
                                "FAILED",
                                "TIMEOUT",
                                "CANCELLED",
                              ].includes(e.status)
                            }
                            title="仅可删除已结束且无关联的记录"
                            onClick={() =>
                              deleteRecord(
                                `/executions/${e.id}`,
                                `${e.task_name} · ${e.period.startsWith("test:") ? "独立测试周期" : e.period}`,
                                "删除此执行的日志、钱包结果和积分历史。正式执行的周期凭据会保留，清理历史后仍禁止同一任务同一周期重复运行。",
                                () => {
                                  if (focused === e.id) {
                                    setFocused(null);
                                    setDetail(null);
                                  }
                                },
                              )
                            }
                          >
                            删除记录
                          </button>
                          {["RUNNING", "QUEUED"].includes(e.status) && (
                            <button
                              onClick={() =>
                                act(() =>
                                  api(`/executions/${e.id}/cancel`, "POST"),
                                )
                              }
                            >
                              停止
                            </button>
                          )}
                          {["FAILED", "TIMEOUT", "PARTIAL_SUCCESS"].includes(
                            e.status,
                          ) && (
                            <button
                              disabled={!executionEnabled}
                              title="当前管理模式未开放重试"
                              onClick={() =>
                                act(() =>
                                  api(`/executions/${e.id}/retry`, "POST"),
                                )
                              }
                            >
                              重试失败钱包
                            </button>
                          )}
                          {["UNKNOWN", "MANUAL_REQUIRED"].includes(
                            e.status,
                          ) && (
                            <button
                              onClick={() =>
                                open({
                                  title: "人工核查未知执行",
                                  initial: {
                                    confirm_mock_runner_stopped: false,
                                    note: "",
                                  },
                                  content: (
                                    <div className="hint">
                                      请先核查并停止对应 Runner
                                      进程。释放钱包锁后保留当前周期，不自动重放。
                                    </div>
                                  ),
                                  fields: [
                                    field(
                                      "confirm_mock_runner_stopped",
                                      "已确认隔离执行进程中的脚本已停止",
                                      "checkbox",
                                      { required: true },
                                    ),
                                    field(
                                      "note",
                                      "核查记录（至少 5 字符）",
                                      "textarea",
                                      { required: true },
                                    ),
                                  ],
                                  submit: (v) =>
                                    api(
                                      `/executions/${e.id}/reconcile`,
                                      "POST",
                                      v,
                                    ),
                                })
                              }
                            >
                              核查
                            </button>
                          )}
                        </div>
                      ),
                    },
                  ]}
                />
              </Panel>
              {focused && detail && (
                <Panel
                  title={detail.task_name + " · 钱包明细"}
                  extra={
                    <button
                      aria-label="关闭执行明细"
                      onClick={() => {
                        setFocused(null);
                        setDetail(null);
                      }}
                    >
                      <X size={18} />
                    </button>
                  }
                >
                  {detail.error && <div className="error">{detail.error}</div>}
                  <div className="results-scroll">
                    <Table
                      rows={detail.results || []}
                      columns={[
                        {
                          title: "钱包",
                          render: (r) => (
                            <>
                              <code title={r.address}>{short(r.address)}</code>
                              <small>{r.name}</small>
                            </>
                          ),
                        },
                        {
                          title: "状态",
                          render: (r) => <Badge status={r.status} />,
                        },
                        { title: "积分", render: (r) => r.points ?? "—" },
                        { title: "原因", render: (r) => r.message || "—" },
                      ]}
                    />
                  </div>
                  <h3 className="log-title">实时日志与执行尝试</h3>
                  {detail.attempts?.map((a: Row) => (
                    <div className="log" key={a.id}>
                      <div>
                        第 {a.number} 次尝试 · {labels[a.status]} · 进程退出码{" "}
                        {a.process_exit_code ?? "尚未退出"}
                      </div>
                      <pre>{a.log || "等待 Runner 输出…"}</pre>
                    </div>
                  ))}
                </Panel>
              )}
              <Panel
                title="自动执行计划"
                extra={
                  <button
                    className="text-button"
                    onClick={() =>
                      act(async () => {
                        show("调度历史", await api("/schedules/history"));
                        return true;
                      })
                    }
                  >
                    调度历史
                    <ArrowRight size={13} />
                  </button>
                }
              >
                <Table
                  rows={data.schedules}
                  columns={[
                    {
                      title: "任务",
                      render: (s) =>
                        data.tasks.find((t: Row) => t.id === s.task_id)?.name,
                    },
                    {
                      title: "时间 / 时区",
                      render: (s) => (
                        <>
                          {String(s.hour).padStart(2, "0")}:
                          {String(s.minute).padStart(2, "0")}
                          <small>
                            {s.timezone} · 星期{" "}
                            {s.weekdays
                              .map(
                                (d: number) =>
                                  ["一", "二", "三", "四", "五", "六", "日"][d],
                              )
                              .join("、")}
                          </small>
                        </>
                      ),
                    },
                    { title: "下次执行", render: (s) => fmt(s.next_run_at) },
                    {
                      title: "状态 / 策略",
                      render: (s) => (
                        <>
                          {s.enabled ? "已启用" : "已暂停"}
                          <small>
                            {s.misfire_policy === "skip"
                              ? "错过跳过"
                              : "补执行一次"}
                          </small>
                        </>
                      ),
                    },
                    {
                      title: "操作",
                      render: (s) => (
                        <div className="actions">
                          <button
                            className="secondary"
                            onClick={() =>
                              act(() =>
                                api("/schedules/" + s.id, "PUT", {
                                  task_id: s.task_id,
                                  hour: s.hour,
                                  minute: s.minute,
                                  timezone: s.timezone,
                                  weekdays: s.weekdays,
                                  misfire_policy: s.misfire_policy,
                                  enabled: !s.enabled,
                                }),
                              )
                            }
                          >
                            {s.enabled ? "暂停" : "主动启用"}
                          </button>
                          <button
                            className="danger-button"
                            onClick={() =>
                              deleteRecord(
                                `/schedules/${s.id}`,
                                `${data.tasks.find((t: Row) => t.id === s.task_id)?.name || "任务"} · ${s.hour}:${String(s.minute).padStart(2, "0")}`,
                                "删除定时计划及其调度历史，停止后续定时触发。已创建的执行继续保留，如需停止请到执行记录中操作。",
                              )
                            }
                          >
                            删除计划
                          </button>
                        </div>
                      ),
                    },
                  ]}
                />
              </Panel>
            </>
          )}
          {page === "settings" && (
            <>
              <div className="settings-grid">
                <section className="panel settings-card">
                  <ShieldCheck className="section-icon" />
                  <h2>安全控制</h2>
                  <p>真实钱包签名与真实交易锁定关闭。</p>
                  <div className="security-state">
                    <span>Emergency Stop</span>
                    <Badge
                      status={
                        data.settings.security?.emergency_stop
                          ? "MANUAL_REQUIRED"
                          : "ONLINE"
                      }
                    />
                  </div>
                  <button
                    className={
                      data.settings.security?.emergency_stop
                        ? "primary"
                        : "danger-button"
                    }
                    onClick={() =>
                      act(() =>
                        api("/settings/security", "PUT", {
                          emergency_stop:
                            !data.settings.security?.emergency_stop,
                        }),
                      )
                    }
                  >
                    {data.settings.security?.emergency_stop
                      ? "解除紧急停止"
                      : "紧急停止全部任务"}
                  </button>
                </section>
                <section className="panel settings-card">
                  <Sparkles className="section-icon" />
                  <h2>AI Provider</h2>
                  <p>
                    {data.settings.ai?.key_configured
                      ? data.settings.ai.model
                      : "规则适配无需密钥即可使用。"}
                  </p>
                  <small>
                    {data.settings.ai?.url || "外部发送前必须审核脱敏预览。"}
                  </small>
                  <button
                    className="secondary"
                    onClick={() =>
                      open({
                        title: "配置兼容 AI API",
                        initial: {
                          url: data.settings.ai?.url || "",
                          model: data.settings.ai?.model || "",
                        },
                        fields: [
                          field("url", "公网 HTTPS API Base URL", "url", {
                            required: true,
                          }),
                          field("model", "模型名称", "text", {
                            required: true,
                          }),
                          field(
                            "api_key",
                            "API Key（加密保存，不回显）",
                            "password",
                            { required: true },
                          ),
                        ],
                        submit: (v) => api("/settings/ai", "PUT", v),
                      })
                    }
                  >
                    配置 API
                  </button>
                  {data.settings.ai?.key_configured && (
                    <button
                      className="danger-button"
                      onClick={() =>
                        deleteRecord(
                          "/settings/ai",
                          "AI API 配置",
                          "清除保存的 API 地址、模型和加密密钥。规则适配仍可使用。",
                        )
                      }
                    >
                      清除 API 配置
                    </button>
                  )}
                </section>
                <section className="panel settings-card">
                  <Activity className="section-icon" />
                  <h2>Worker 与并发</h2>
                  <p>
                    保守全局并发：
                    {data.settings.execution?.global_concurrency || 2}
                  </p>
                  <select
                    aria-label="全局并发"
                    value={data.settings.execution?.global_concurrency || 2}
                    onChange={(e) =>
                      act(() =>
                        api("/settings/execution", "PUT", {
                          global_concurrency: Number(e.target.value),
                        }),
                      )
                    }
                  >
                    <option value={1}>1 个执行</option>
                    <option value={2}>2 个执行</option>
                  </select>
                  {data.settings.workers?.map((w: Row) => (
                    <div className="status-line" key={w.id}>
                      <span>{w.id}</span>
                      <Badge status={w.status} />
                    </div>
                  ))}
                </section>
                <section className="panel settings-card">
                  <Wallet className="section-icon" />
                  <h2>备份与告警</h2>
                  <p>
                    {data.settings.backup?.verified || 0} /{" "}
                    {data.settings.backup?.wallets || 0} 钱包已验证恢复
                  </p>
                  <small>
                    异常任务写入后台审计日志。外部邮件通知尚未接入。
                  </small>
                  <button className="secondary" onClick={backup}>
                    下载加密备份
                  </button>
                </section>
              </div>
              <Panel
                title="安全审计记录"
                extra={<span className="muted">最近 200 条</span>}
              >
                <Table
                  rows={data.audit}
                  columns={[
                    { title: "操作", render: (a) => a.action },
                    {
                      title: "目标",
                      render: (a) => <code>{short(a.target)}</code>,
                    },
                    { title: "时间", render: (a) => fmt(a.created_at) },
                    { title: "详情", render: (a) => JSON.stringify(a.detail) },
                  ]}
                />
              </Panel>
            </>
          )}
          <footer>
            <span>AirDrop V2 · 通用 Web3 自动化平台</span>
            <span>管理模式 · 执行未开放 · Asia/Shanghai</span>
          </footer>
        </main>
      </div>
      <Dialog.Root open={!!spec} onOpenChange={(o) => !o && setSpec(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="overlay" />
          <Dialog.Content className="modal">
            <div className="modal-head">
              <Dialog.Title>{spec?.title}</Dialog.Title>
              <Dialog.Close aria-label="关闭">
                <X size={20} />
              </Dialog.Close>
            </div>
            <Dialog.Description className="sr-only">
              查看内容或填写表单并提交。
            </Dialog.Description>
            <form onSubmit={submit}>
              {error && (
                <div className="error" role="alert">
                  {error}
                </div>
              )}
              {spec?.content}
              {spec?.fields?.map((f) =>
                f.type === "wallet-group" ? (
                  <WalletGroupPicker
                    key={f.key}
                    groups={data.groups}
                    value={values[f.key] || ""}
                    name={values.group_name || ""}
                    onSelect={(value) => put(f.key, value)}
                    onName={(value) => put("group_name", value)}
                  />
                ) : (
                  <label
                    className={f.type === "checkbox" ? "checkbox" : "field"}
                    key={f.key}
                  >
                    {f.type !== "checkbox" && <span>{f.label}</span>}
                    {f.type === "checkbox" ? (
                      <>
                        <input
                          type="checkbox"
                          aria-label={f.label}
                          required={f.required}
                          disabled={f.disabled}
                          checked={!!values[f.key]}
                          onChange={(e) => put(f.key, e.target.checked)}
                        />
                        {f.label}
                      </>
                    ) : f.type === "multi" ? (
                      <div className="check-list">
                        {f.options?.map((o) => (
                          <div className="check-row" key={o.value}>
                            <input
                              type="checkbox"
                              aria-label={o.label}
                              checked={
                                values[f.key]?.includes(o.value) || false
                              }
                              onChange={(e) =>
                                put(
                                  f.key,
                                  e.target.checked
                                    ? [...(values[f.key] || []), o.value]
                                    : (values[f.key] || []).filter(
                                        (v: string) => v !== o.value,
                                      ),
                                )
                              }
                            />
                            {o.label}
                          </div>
                        ))}
                      </div>
                    ) : f.type === "select" ? (
                      <select
                        aria-label={f.label}
                        required={f.required}
                        value={values[f.key] || ""}
                        onChange={(e) => put(f.key, e.target.value)}
                      >
                        <option value="">请选择</option>
                        {f.options?.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    ) : f.type === "textarea" ? (
                      <textarea
                        aria-label={f.label}
                        required={f.required}
                        value={values[f.key] || ""}
                        onChange={(e) => put(f.key, e.target.value)}
                      />
                    ) : f.type === "file" ? (
                      <input
                        type="file"
                        aria-label={f.label}
                        required={f.required}
                        onChange={(e) => put(f.key, e.target.files?.[0])}
                      />
                    ) : (
                      <input
                        type={f.type || "text"}
                        aria-label={f.label}
                        required={f.required}
                        min={f.min}
                        max={f.max}
                        value={values[f.key] ?? ""}
                        onChange={(e) => put(f.key, e.target.value)}
                      />
                    )}{" "}
                    {f.note && <small>{f.note}</small>}
                  </label>
                ),
              )}
              <div className="modal-footer">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setSpec(null)}
                >
                  {spec?.submit ? "取消" : "关闭"}
                </button>
                {spec?.submit && (
                  <button
                    className={spec.destructive ? "danger-button" : "primary"}
                    disabled={busy}
                  >
                    {busy ? "处理中…" : spec.button || "确认提交"}
                    <ArrowRight size={14} />
                  </button>
                )}
              </div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
