import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownToLine,
  Pause,
  Play,
  RotateCw,
  Search,
  Upload,
} from "lucide-react";

type Row = Record<string, any>;

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
  return r.headers.get("content-disposition")?.includes("attachment") ||
    r.headers.get("content-type")?.includes("application/octet-stream")
    ? r.blob()
    : r.json();
}

/** 记录当前会话的 CSRF 令牌；主界面登录后调用一次即可。 */
export function setLogCsrf(value: string) {
  csrf = value;
}

export function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const labels: Row = {
  COMPLETE: "日志完整",
  IN_PROGRESS: "日志写入中",
  STORAGE_FAILED: "日志存储故障",
  PARTIAL: "日志不完整",
};

// 浏览器不保存整份大日志：超过这个长度就丢掉最早的一段，允许再向前加载。
const MAX_WINDOW = 512 * 1024;
const PAGE_BYTES = 64 * 1024;
const ACTIVE = new Set([
  "QUEUED",
  "PREPARING",
  "RUNNING",
  "STOPPING",
  "RECONCILING",
]);

function highlight(text: string, needle: string) {
  if (!needle) return text;
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  return parts.map((part, index) =>
    part.toLowerCase() === needle.toLowerCase() ? (
      <mark key={index}>{part}</mark>
    ) : (
      <React.Fragment key={index}>{part}</React.Fragment>
    ),
  );
}

export default function ExecutionLog({
  executionId,
  attemptId,
  status,
  logStatus,
  compact = false,
}: {
  executionId: string;
  attemptId?: string;
  status: string;
  logStatus?: string;
  compact?: boolean;
}) {
  const [text, setText] = useState("");
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(0);
  const [total, setTotal] = useState(0);
  const [gap, setGap] = useState(false);
  const [complete, setComplete] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [follow, setFollow] = useState(true);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLPreElement | null>(null);
  const windowRef = useRef({ start: 0, end: 0, total: 0 });
  const textRef = useRef("");
  windowRef.current = { start, end, total };
  textRef.current = text;

  function applyStart(value: number) {
    windowRef.current.start = value;
    setStart(value);
  }
  function applyEnd(value: number) {
    windowRef.current.end = value;
    setEnd(value);
  }
  function applyTotal(value: number) {
    windowRef.current.total = value;
    setTotal(value);
  }
  function trim(value: string, from: number) {
    const binary = new TextEncoder().encode(value);
    if (binary.length <= MAX_WINDOW) return { value, from, bytes: binary.length };
    const kept = binary.slice(binary.length - MAX_WINDOW);
    let begin = 0;
    while (begin < kept.length && kept[begin] & 0xc0) begin += 1;
    const cut = binary.length - kept.length + begin;
    return {
      value: new TextDecoder().decode(binary.slice(cut, binary.length)),
      from: from + cut,
      bytes: binary.length - cut,
    };
  }

  function trimEnd(value: string) {
    const binary = new TextEncoder().encode(value);
    if (binary.length <= MAX_WINDOW) return { value, bytes: binary.length };
    let end = MAX_WINDOW;
    while (end > 0 && end < binary.length && (binary[end] & 0xc0) === 0x80) {
      end -= 1;
    }
    const kept = binary.slice(0, end);
    return { value: new TextDecoder().decode(kept), bytes: kept.length };
  }

  async function fetchLog(offset: number) {
    const params = new URLSearchParams({
      cursor: String(offset),
      limit_bytes: String(PAGE_BYTES),
    });
    if (attemptId) params.set("attempt_id", attemptId);
    return api(`/executions/${executionId}/logs?${params.toString()}`);
  }

  /** 初次打开：直接读日志尾部，服务端按执行记录定位，刷新后不依赖浏览器内存。 */
  async function pullFirst() {
    const head = await fetchLog(0);
    if (!head.chunks.length && !head.log_bytes) {
      setText("");
      applyStart(0);
      applyEnd(0);
      applyTotal(0);
      setComplete(false);
      setTruncated(false);
      return;
    }
    const marker = Math.max(0, Number(head.log_bytes) - PAGE_BYTES);
    const page = marker > 0 ? await fetchLog(marker) : head;
    const value = page.chunks.map((c: Row) => c.text).join("");
    const first = page.chunks.length ? Number(page.chunks[0].offset) : 0;
    setText(value);
    applyStart(first);
    applyEnd(Number(page.cursor));
    applyTotal(Number(page.log_bytes));
    setComplete(!!page.finished);
    setTruncated(marker > 0);
  }

  /** 运行中只把新内容接到尾部；重复分段由服务端游标去重。 */
  async function pullTail() {
    const page = await fetchLog(windowRef.current.end);
    const value = page.chunks.map((c: Row) => c.text).join("");
    const merged = textRef.current + value;
    const kept = trim(merged, windowRef.current.start);
    if (kept.from > windowRef.current.start) {
      applyStart(kept.from);
      setTruncated(true);
    }
    setText(kept.value);
    applyEnd(Number(page.cursor));
    applyTotal(Number(page.log_bytes));
    if (page.gap) setGap(true);
    setComplete(!!page.finished);
  }

  async function loadEarlier() {
    if (!windowRef.current.start || busy) return;
    setFollow(false);
    setBusy(true);
    try {
      const offset = Math.max(0, windowRef.current.start - PAGE_BYTES);
      const page = await fetchLog(offset);
      const value = page.chunks.map((c: Row) => c.text).join("");
      const first = page.chunks.length
        ? Number(page.chunks[0].offset)
        : windowRef.current.start;
      const merged = trimEnd(value + textRef.current);
      setText(merged.value);
      applyStart(first);
      applyEnd(first + merged.bytes);
      if (first + merged.bytes < windowRef.current.total) setTruncated(true);
      if (page.gap) setGap(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    try {
      setError("");
      await pullFirst();
      setFollow(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    setText("");
    applyStart(0);
    applyEnd(0);
    applyTotal(0);
    setGap(false);
    setComplete(false);
    setTruncated(false);
    setFollow(true);
    pullFirst().catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [executionId, attemptId]);

  const active = ACTIVE.has(status);
  useEffect(() => {
    // 运行中约 1 秒轮询；空闲时降低频率并停止追赶（暂停跟随即停止轮询）。
    if (!follow) return;
    if (complete && !active) return;
    const delay = active ? 1000 : 12000;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await pullTail();
      } catch (e) {
        setError((e as Error).message);
      }
      if (!cancelled) timer = setTimeout(poll, delay);
    };
    timer = setTimeout(poll, delay);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // Pull state comes from refs so an idle poll keeps running without a render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [follow, complete, active, executionId, attemptId, status]);

  useEffect(() => {
    if (!follow || !box.current) return;
    box.current.scrollTop = box.current.scrollHeight;
  }, [follow, text]);

  const rendered = useMemo(() => highlight(text, query.trim()), [text, query]);
  const hiddenEarlier = Math.max(0, start);
  const hiddenLater = Math.max(0, total - end);

  return (
    <div className={`exec-log${compact ? " compact" : ""}`}>
      <div className="exec-log-bar">
        <button
          className="secondary"
          aria-pressed={follow}
          onClick={() => setFollow((value) => !value)}
        >
          {follow ? <Pause size={14} /> : <Play size={14} />}
          {follow ? "暂停滚动" : "跟随输出"}
        </button>
        <label className="exec-log-search">
          <Search size={14} />
          <input
            aria-label="搜索已加载日志"
            placeholder="搜索已加载内容"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button onClick={loadEarlier} disabled={busy || start === 0}>
          <Upload size={14} />
          加载较早内容
        </button>
        <button onClick={refresh} disabled={busy}>
          <RotateCw size={14} />
          重新读取
        </button>
        <button
          onClick={() =>
            api(
              `/executions/${executionId}/logs/download${attemptId ? `?attempt_id=${attemptId}` : ""}`,
            )
              .then((blob) =>
                downloadBlob(blob as Blob, `airdrop-${executionId}.log`),
              )
              .catch((e) => setError(e.message))
          }
        >
          <ArrowDownToLine size={14} />
          完整下载
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {gap && (
        <div className="hint" role="status">
          日志存在缺口：中间有分段缺失或校验失败，平台不会把这条记录当作完整日志。
        </div>
      )}
        {hiddenEarlier > 0 && (
        <div className="muted exec-log-note">
          前面还有 {hiddenEarlier} 字节未加载，可点击“加载较早内容”。
        </div>
      )}
      {hiddenLater > 0 && !follow && (
        <div className="muted exec-log-note">
          后面还有 {hiddenLater} 字节未显示，可点击“跟随输出”继续读取。
        </div>
      )}
      <pre ref={box} className="exec-log-box">
        {rendered || "等待脚本输出…"}
      </pre>
      <div className="exec-log-foot">
        <span className={`badge ${logStatus === "COMPLETE" ? "green" : "gray"}`}>
          <i />
          {labels[logStatus || ""] || "日志状态未知"}
        </span>
        <span className="muted">
          已加载第 {start}–{end} 字节 / 共 {total} 字节
          {truncated && !complete ? " · 页面只保留最近一段" : ""}
        </span>
      </div>
      <p className="muted">
        游标按字节增量读取，平台事件与脚本输出分开标注；日志经脱敏后保存，脚本刻意编码的秘密无法保证完全识别。
      </p>
    </div>
  );
}
