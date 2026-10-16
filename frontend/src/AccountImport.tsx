import React, { useEffect, useState } from "react";
import { Check, RefreshCw, UploadCloud } from "lucide-react";

type Row = Record<string, any>;

const GROUPS = [
  { key: "new", title: "新增", tone: "green" },
  { key: "reuse", title: "复用", tone: "gray" },
  { key: "conflict", title: "冲突", tone: "red" },
  { key: "error", title: "错误", tone: "red" },
] as const;

const short = (value?: string, size = 16) =>
  value && value.length > size ? value.slice(0, size) + "…" : value || "—";

const fmt = (value?: number) =>
  value ? new Date(value * 1000).toLocaleString("zh-CN") : "—";

export default function AccountImport({
  project,
  api,
  onClose,
  onChanged,
}: {
  project: Row;
  api: (path: string, method?: string, body?: any) => Promise<any>;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const [features, setFeatures] = useState<Row[]>([]);
  const [featureId, setFeatureId] = useState("");
  const [chain, setChain] = useState("EVM");
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState("{}");
  const [draft, setDraft] = useState<Row | null>(null);
  const [decisions, setDecisions] = useState<Row>({});
  const [accounts, setAccounts] = useState<Row[] | null>(null);
  const [result, setResult] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const feature = features.find((f: Row) => f.id === featureId);
  const packageId = feature?.package_id || "";

  useEffect(() => {
    let alive = true;
    // 资料导入按脚本功能定位字段语义，因此只列出已生成运行定义的功能。
    api("/scripts")
      .then(async (versions: Row[]) => {
        const groups = await Promise.all(
          versions
            .filter((v: Row) => v.feature_count)
            .map(async (v: Row) => {
              const list = await api(`/scripts/${v.id}/features`);
              return list.map((f: Row) => ({
                ...f,
                package_id: v.package_id,
                version_id: v.id,
                version_name: `${v.name} v${v.version}`,
              }));
            }),
        );
        if (alive) setFeatures(groups.flat());
      })
      .catch((e) => setError(e.message));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function act(fn: () => Promise<any>) {
    setBusy(true);
    setError("");
    try {
      return await fn();
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function loadAccounts(id: string) {
    if (!id) {
      setAccounts(null);
      return;
    }
    setAccounts(await api(`/projects/${project.id}/accounts?package_id=${id}`));
  }

  useEffect(() => {
    loadAccounts(packageId).catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [packageId]);

  async function preview() {
    if (!packageId || !file) {
      setError("请选择脚本功能并上传 CSV 资料文件。");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(mapping);
    } catch {
      setError("字段映射不是合法 JSON。");
      return;
    }
    const body = new FormData();
    body.append("package_id", packageId);
    body.append("chain", chain);
    body.append("field_mapping", JSON.stringify(parsed));
    body.append("file", file);
    const created = await act(() =>
      api(`/projects/${project.id}/account-imports`, "POST", body),
    );
    if (created) {
      setDraft(created);
      setDecisions({});
      setResult(null);
      setNotice("已生成遮蔽预览；敏感字段只显示是否存在，冲突必须逐项选择。");
    }
  }

  async function commit() {
    if (!draft) return;
    const undone = (draft.preview?.entries || []).filter(
      (entry: Row) => entry.group === "conflict" && !decisions[entry.address],
    );
    if (undone.length) {
      setError(
        `还有 ${undone.length} 个冲突项未选择处理方式，请先逐项确认。`,
      );
      return;
    }
    const committed = await act(() =>
      api(`/account-imports/${draft.id}/commit`, "POST", {
        preview_sha256: draft.preview_sha256,
        decisions,
        confirmed: true,
      }),
    );
    if (committed) {
      setResult(committed);
      setDraft(null);
      setFile(null);
      setNotice("导入已完成；重复确认同一草稿会返回同一结果。");
      await loadAccounts(packageId);
      onChanged?.();
    }
  }

  const entries: Row[] = draft?.preview?.entries || [];
  const errors: Row[] = draft?.preview?.errors || [];
  const counts = draft?.preview?.counts || {};

  return (
    <div className="overlay" role="dialog" aria-label="项目账号资料导入">
      <div className="modal account-import">
        <div className="modal-head">
          <h2>项目资料导入 · {project.name}</h2>
          <button aria-label="关闭项目资料导入" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="account-import-body">
          {error && (
            <div className="error" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="hint" role="status">
              {notice}
            </div>
          )}
          <div className="hint">
            导入只读取 CSV
            文本，私钥等敏感字段加密保存并以遮蔽值展示；预览摘要绑定原数据修订号，资料在预览后被修改时必须重新导入。
          </div>
          <div className="account-import-grid">
            <label className="field">
              <span>脚本功能</span>
              <select
                aria-label="导入目标脚本功能"
                value={featureId}
                onChange={(e) => {
                  setFeatureId(e.target.value);
                  setDraft(null);
                  setResult(null);
                }}
              >
                <option value="">选择已审核功能</option>
                {features.map((f: Row) => (
                  <option key={f.id} value={f.id}>
                    {(f.definition?.name || f.id) +
                      " · " +
                      (f.version_name || "")}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>链</span>
              <select
                aria-label="导入资料链"
                value={chain}
                onChange={(e) => setChain(e.target.value)}
              >
                <option value="EVM">EVM</option>
                <option value="SOLANA">Solana</option>
              </select>
            </label>
            <label className="field">
              <span>资料文件（CSV）</span>
              <input
                type="file"
                aria-label="项目资料文件"
                accept=".csv,text/csv"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
            </label>
            <label className="field">
              <span>字段映射（CSV 列名 → 目标字段）</span>
              <textarea
                aria-label="字段映射"
                value={mapping}
                spellCheck={false}
                onChange={(e) => setMapping(e.target.value)}
              />
              <small>
                例：{`{"地址列":"address","私钥列":"private_key"}`}。地址与私钥不一致、地址重复都会进入错误分组。
              </small>
            </label>
          </div>
          <div className="account-import-actions">
            <button
              className="primary"
              disabled={busy || !packageId || !file}
              title={
                packageId && file
                  ? "上传并生成预览"
                  : "请先选择脚本功能和 CSV 文件"
              }
              onClick={preview}
            >
              <UploadCloud size={14} />
              上传并预览
            </button>
            <button
              disabled={busy || !packageId}
              onClick={() =>
                act(() => loadAccounts(packageId)).then(() =>
                  setNotice("已刷新该项目/脚本的账号资料。"),
                )
              }
            >
              <RefreshCw size={14} />
              刷新资料
            </button>
          </div>
          {draft && (
            <>
              <h3>导入预览</h3>
              <div className="account-summary">
                {GROUPS.map((group) => (
                  <span key={group.key} className={"badge " + group.tone}>
                    <i />
                    {group.title} {counts[group.key] || 0}
                  </span>
                ))}
                <span className="muted">文件 {draft.filename}</span>
                <span className="muted" title={draft.preview_sha256}>
                  预览摘要 {short(draft.preview_sha256, 16)}
                </span>
              </div>
              <p className="muted">
                {draft.preview?.note ||
                  "空值表示未提供，不会清空已有值；清空必须显式选择。"}
              </p>
              <div className="account-entries">
                {entries.map((entry: Row) => (
                  <div className="account-entry" key={entry.address}>
                    <div className="account-entry-head">
                      <code title={entry.address}>{short(entry.address, 22)}</code>
                      <span
                        className={
                          "badge " +
                          (entry.group === "new"
                            ? "green"
                            : entry.group === "conflict"
                              ? "red"
                              : "gray")
                        }
                      >
                        <i />
                        {
                          GROUPS.find((g) => g.key === entry.group)?.title
                        }
                      </span>
                      {entry.has_private_key && (
                        <span className="badge gray">
                          <i />
                          含私钥
                        </span>
                      )}
                      {entry.revision !== null && entry.revision !== undefined && (
                        <small>修订号 {entry.revision}</small>
                      )}
                    </div>
                    <div className="account-values">
                      {Object.entries(entry.values || {}).map(([name, value]) => (
                        <span key={name}>
                          {name}：{String(value) || "（空）"}
                        </span>
                      ))}
                    </div>
                    {entry.conflicts?.length > 0 && (
                      <div className="account-conflict">
                        {entry.conflicts.map((conflict: Row) => (
                          <div key={conflict.field}>
                            <span>
                              {conflict.field}：现有{" "}
                              <code>{conflict.current || "（空）"}</code> → 导入{" "}
                              <code>{conflict.incoming || "（空）"}</code>
                            </span>
                          </div>
                        ))}
                        <div className="account-decision">
                          {(
                            [
                              ["overwrite", "覆盖"],
                              ["skip", "跳过此项"],
                              ["clear", "清空已有值"],
                            ] as const
                          ).map(([value, label]) => (
                            <label key={value} className="checkbox">
                              <input
                                type="radio"
                                name={`decision-${entry.address}`}
                                aria-label={`${short(entry.address, 10)} ${label}`}
                                checked={decisions[entry.address] === value}
                                onChange={() =>
                                  setDecisions((d) => ({
                                    ...d,
                                    [entry.address]: value,
                                  }))
                                }
                              />
                              {label}
                            </label>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              {errors.length > 0 && (
                <div className="error">
                  <strong>以下行不会导入：</strong>
                  {errors.map((item: Row) => (
                    <div key={item.row}>
                      第 {item.row} 行：{item.reason}
                    </div>
                  ))}
                </div>
              )}
              <div className="modal-footer">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => {
                    setDraft(null);
                    setDecisions({});
                  }}
                >
                  放弃此预览
                </button>
                <button className="primary" disabled={busy} onClick={commit}>
                  <Check size={14} />
                  {busy ? "处理中…" : "确认导入"}
                </button>
              </div>
            </>
          )}
          {result && (
            <div className="hint" role="status">
              导入结果：新增 {result.created} 条、更新 {result.updated} 条、跳过{" "}
              {result.skipped} 条、新建钱包 {result.wallets_created} 个。
            </div>
          )}
          {packageId && (
            <>
              <h3>现有账号资料</h3>
              {accounts === null && <p className="muted">正在读取…</p>}
              {accounts?.length === 0 && (
                <div className="empty">
                  <strong>该项目/脚本还没有账号资料</strong>
                  <p>上传 CSV 并完成预览后，这里会显示资料完整性与来源。</p>
                </div>
              )}
              {!!accounts?.length && (
                <div className="results-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>地址</th>
                        <th>名称 / 链</th>
                        <th>公开字段</th>
                        <th>敏感字段</th>
                        <th>修订号 / 来源</th>
                        <th>更新时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {accounts.map((account: Row) => (
                        <tr key={account.id}>
                          <td>
                            <code title={account.address}>
                              {short(account.address, 18)}
                            </code>
                          </td>
                          <td>
                            {account.name || "未命名"}
                            <small>{account.chain}</small>
                          </td>
                          <td>
                            {Object.entries(account.public_config || {})
                              .map(([name, value]) => `${name}=${String(value) || "空"}`)
                              .join(" · ") || "—"}
                          </td>
                          <td>
                            {account.has_secrets ? (
                              Object.entries(account.secured_config || {})
                                .map(([name, value]) => `${name}=${value}`)
                                .join(" · ")
                            ) : (
                              <span className="muted">仅有身份字段</span>
                            )}
                          </td>
                          <td>
                            修订 {account.revision}
                            <small>{account.source || "—"}</small>
                          </td>
                          <td>{fmt(account.updated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
