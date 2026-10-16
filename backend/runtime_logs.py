"""完整日志：加密分段、游标读取与下载。

控制器已按到达顺序增量脱敏，本模块只负责把脱敏文本切成固定大小的分段，
每段独立加密保存摘要、序号与字节范围；数据库保留索引和短尾部，不保存整段
大日志文本。读取按字节游标增量返回；重复分段不重复显示；缺失分段显式标注，
不做静默截断。
"""
import codecs
import hashlib
from fastapi import HTTPException
from sqlalchemy import select
from backend.config import settings
from backend.models import Artifact, Execution, ExecutionAttempt, ExecutionLogSegment
from backend.storage import read_blob, write_blob

FINAL_LOG_STATUS = {'COMPLETE', 'GAP', 'STORAGE_FAILED'}
TAIL_CHARS = 8000


def attempt_of(db, execution: Execution, attempt_id: str | None = None) -> ExecutionAttempt:
    identity = attempt_id or execution.current_attempt_id
    attempt = db.get(ExecutionAttempt, identity) if identity else None
    if attempt is not None and attempt.execution_id != execution.id:
        attempt = None
    if attempt is None:
        attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution.id)
                            .order_by(ExecutionAttempt.number.desc()).limit(1))
    if attempt is None:
        raise HTTPException(404, '该执行还没有运行尝试记录')
    return attempt


def segments_of(db, attempt_id: str):
    return list(db.scalars(select(ExecutionLogSegment).where(ExecutionLogSegment.attempt_id == attempt_id)
                           .order_by(ExecutionLogSegment.seq)))


def segment_bytes(db, segment: ExecutionLogSegment) -> bytes | None:
    """单段解密；工件缺失或校验失败返回 None，由调用方显式标注缺口。"""
    artifact = db.get(Artifact, segment.artifact_id)
    if artifact is None:
        return None
    try:
        return read_blob(artifact.path, segment.artifact_sha256)
    except (OSError, ValueError):
        return None


def append_text(db, attempt_id: str, seq: int, text: str) -> dict:
    """追加一段脱敏文本；同一 (attempt, seq) 重复提交按摘要去重。"""
    data = text.encode('utf-8')
    if not data:
        raise ValueError('日志分段不能为空')
    if seq < 0:
        raise ValueError('日志分段序号不合法')
    attempt = db.get(ExecutionAttempt, attempt_id)
    if attempt is None:
        raise ValueError('运行尝试不存在，无法保存日志分段')
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(ExecutionLogSegment).where(ExecutionLogSegment.attempt_id == attempt_id,
                                                           ExecutionLogSegment.seq == seq))
    if existing is not None:
        if existing.artifact_sha256 != digest:
            raise ValueError('同一序号收到了不同内容的日志分段，已拒绝覆盖')
        return {'seq': seq, 'offset': existing.offset_bytes, 'size': existing.size_bytes, 'deduped': True}
    offset = int(attempt.log_bytes or 0)
    path, stored = write_blob(data)
    artifact = Artifact(path=path, sha256=stored, kind='execution-log')
    db.add(artifact)
    db.flush()
    db.add(ExecutionLogSegment(attempt_id=attempt_id, seq=seq, artifact_id=artifact.id, artifact_sha256=stored,
                               offset_bytes=offset, size_bytes=len(data)))
    attempt.log_bytes = offset + len(data)
    # 短尾部保留给旧界面与快速排查；完整内容以加密分段为准。
    attempt.log = ((attempt.log or '') + text)[-TAIL_CHARS:]
    return {'seq': seq, 'offset': offset, 'size': len(data), 'sha256': stored, 'deduped': False}


def char_start(data: bytes, begin: int) -> int:
    """把字节游标回退到字符起始位置，避免从字符中间开始读出乱码。"""
    begin = min(max(begin, 0), len(data))
    while begin > 0 and begin < len(data) and data[begin] & 0xC0 == 0x80:
        begin -= 1
    return begin


def take_text(data: bytes, begin: int, limit: int) -> tuple[str, int]:
    """按字节上限取出一段完整 UTF-8 文本，绝不切开字符，也不丢任何一个字节。

    游标是字节口径；把多字节字符切成两半会让界面出现乱码并让后续页错位，
    因此这里按“已完整解码的字节数”推进游标，宁可少取几个字节。上限小于一个
    字符的字节数时至少推进一个完整字符，保证任何游标都能前进。
    """
    window = data[begin:begin + limit]
    if not window:
        return '', 0
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    text = decoder.decode(window, False)
    taken = len(window) - len(decoder.getstate()[0])
    if taken:
        return text, taken
    for size in (2, 3, 4):
        candidate = data[begin:begin + size]
        if len(candidate) < size:
            break
        try:
            return candidate.decode('utf-8'), size
        except UnicodeDecodeError:
            continue
    # 非法字节：按单字节推进并显式替换，保证任何输入都能继续。
    return data[begin:begin + 1].decode('utf-8', 'replace'), 1


def page(db, execution: Execution, attempt: ExecutionAttempt, offset: int = 0, limit_bytes: int | None = None) -> dict:
    """按字节游标读取；返回下一游标、是否结束与日志完整性。"""
    limit = max(1, min(int(limit_bytes or settings.native_log_page_bytes), settings.native_log_page_limit))
    position = max(0, int(offset or 0))
    total = int(attempt.log_bytes or 0)
    chunks, remaining, gap = [], limit, False
    stored = segments_of(db, attempt.id)
    reachable = 0
    index = 0
    while index < len(stored) and remaining > 0:
        segment = stored[index]
        end = segment.offset_bytes + segment.size_bytes
        if segment.offset_bytes > reachable:
            # 上一段与这一段之间有空隙（分段被清理过）：显式记录缺口，不伪造内容。
            gap = True
            position = max(position, segment.offset_bytes)
        reachable = max(reachable, end)
        if position >= end:
            # 这一段已经全部读过（或已被缺口跳过）。
            index += 1
            continue
        position = max(position, segment.offset_bytes)
        data = segment_bytes(db, segment)
        if data is None:
            gap = True
            chunks.append({'offset': segment.offset_bytes,
                           'text': f'[平台] 日志分段 #{segment.seq} 缺失或校验失败，这一段无法显示。\n'})
            position = end
            index += 1
            continue
        # 游标可能落在字符中间（例如旧版本写入或人为跳转）：先对齐到字符起点。
        begin = char_start(data, position - segment.offset_bytes)
        text_value, taken = take_text(data, begin, remaining)
        if not taken:
            position = end
            index += 1
            continue
        chunks.append({'offset': segment.offset_bytes + begin, 'text': text_value})
        position = segment.offset_bytes + begin + taken
        remaining -= taken
        # 只有这一段读完之后才换下一段；分页额度用尽时游标停在字符边界上。
        if position >= end:
            index += 1
    if not stored and attempt.log:
        # 旧模拟运行只保留短尾部文本；按单段返回，保持界面一致。
        raw = attempt.log.encode('utf-8')
        if position == 0:
            chunks.append({'offset': 0, 'text': attempt.log})
        position, total = len(raw), len(raw)
    status = execution.log_status or ''
    finished = position >= total and status in FINAL_LOG_STATUS
    return {'attempt_id': attempt.id, 'chunks': chunks, 'cursor': position, 'log_bytes': total,
            'log_status': status, 'gap': gap, 'finished': finished,
            'truncated': position < total, 'segment_count': len(stored)}


def download_chunks(execution_id: str, attempt_id: str):
    """按序解密并流式产出文本；生成器自开会话，不依赖请求会话的生命周期。"""
    from backend.db import Session
    with Session() as db:
        attempt = db.get(ExecutionAttempt, attempt_id)
        if attempt is None or attempt.execution_id != execution_id:
            raise HTTPException(404, '运行尝试不存在')
        stored = segments_of(db, attempt.id)
        if not stored:
            if attempt.log:
                yield attempt.log
            return
        for segment in stored:
            data = segment_bytes(db, segment)
            if data is None:
                yield f'\n[平台] 日志分段 #{segment.seq} 缺失或校验失败，这一段无法下载。\n'
                continue
            yield data.decode('utf-8', 'replace')


class SegmentWriter:
    """把一个 attempt 的脱敏输出按序切成 256 KiB 分段。

    单个分段写入失败即记录失败原因，调用方必须据此停止容器并标记日志存储
    失败，不能把有缺口的记录当作完整日志。
    """

    def __init__(self, execution_id: str, attempt_id: str):
        self.execution_id, self.attempt_id = execution_id, attempt_id
        self.buffer = ''
        self.seq = 0
        self.failure = ''
        self.bytes = 0

    def feed(self, text: str):
        if self.failure or not text:
            return
        self.buffer += text
        self._drain()

    def flush(self):
        if self.failure:
            return
        self._drain()
        if self.buffer:
            pending, self.buffer = self.buffer, ''
            self._write(pending)

    def _drain(self):
        size = settings.native_log_segment_bytes
        data = self.buffer.encode('utf-8')
        while len(data) >= size:
            text = data[:size].decode('utf-8', 'ignore')
            consumed = len(text.encode('utf-8'))
            if not consumed:
                break
            self._write(text)
            if self.failure:
                return
            data = data[consumed:]
        self.buffer = data.decode('utf-8', 'replace')

    def _write(self, text: str):
        from backend.db import Session
        from backend.queue import locked_db
        try:
            with locked_db() as db:
                append_text(db, self.attempt_id, self.seq, text)
        except Exception as exc:  # 保存失败必须停止运行并显式标记
            self.failure = f'{type(exc).__name__}: {exc}'[:300]
            return
        self.seq += 1
        self.bytes += len(text.encode('utf-8'))
