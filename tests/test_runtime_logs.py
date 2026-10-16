"""完整日志：加密分段、游标分页、缺口标注与下载。"""
from pathlib import Path
import pytest
from sqlalchemy import select
from backend.config import settings
from backend.db import Session
from backend.models import Artifact, Execution, ExecutionAttempt
from backend import runtime_logs
from tests.test_runtime_state import fixture as state_fixture


@pytest.fixture
def ledger():
    """一个执行与它的运行尝试；日志只挂在尝试上，不写进执行记录。"""
    data = state_fixture()
    with Session.begin() as db:
        attempt = ExecutionAttempt(execution_id=data['execution'], number=1)
        db.add(attempt)
        db.flush()
        db.get(Execution, data['execution']).current_attempt_id = attempt.id
    return {**data, 'attempt': attempt.id}


def append(ledger, parts):
    with Session.begin() as db:
        for seq, part in enumerate(parts):
            runtime_logs.append_text(db, ledger['attempt'], seq, part)


def read_page(ledger, cursor=0, limit=None):
    with Session() as db:
        execution = db.get(Execution, ledger['execution'])
        attempt = db.get(ExecutionAttempt, ledger['attempt'])
        return runtime_logs.page(db, execution, attempt, cursor, limit)


def read_all(ledger, limit=16):
    text, cursor, pages = '', 0, 0
    while True:
        page = read_page(ledger, cursor, limit)
        text += ''.join(chunk['text'] for chunk in page['chunks'])
        pages += 1
        assert page['cursor'] >= cursor
        if page['cursor'] == cursor:
            break
        cursor = page['cursor']
        if not page['truncated']:
            break
    return text, pages, cursor


def test_segments_are_encrypted_deduped_and_keep_a_short_tail(ledger):
    text = '第一步完成\n第二步开始\n'
    with Session.begin() as db:
        stored = runtime_logs.append_text(db, ledger['attempt'], 0, text)
        assert stored == {'seq': 0, 'offset': 0, 'size': len(text.encode()), 'sha256': stored['sha256'],
                          'deduped': False}
        repeat = runtime_logs.append_text(db, ledger['attempt'], 0, text)
        assert repeat['deduped'] is True and repeat['offset'] == 0
        with pytest.raises(ValueError, match='不同内容'):
            runtime_logs.append_text(db, ledger['attempt'], 0, text + '改动')
        second = runtime_logs.append_text(db, ledger['attempt'], 1, '第三步\n')
        assert second['offset'] == len(text.encode())
    with Session() as db:
        attempt = db.get(ExecutionAttempt, ledger['attempt'])
        assert attempt.log_bytes == len((text + '第三步\n').encode())
        # 数据库只保留短尾部，整段内容以加密分段为准。
        assert attempt.log.endswith('第三步\n') and len(attempt.log) < len(text) + 10
        segments = runtime_logs.segments_of(db, ledger['attempt'])
        assert [segment.seq for segment in segments] == [0, 1]
        artifact = db.get(Artifact, segments[0].artifact_id)
        assert '第一步'.encode() not in Path(artifact.path).read_bytes()
        assert runtime_logs.segment_bytes(db, segments[0]) == text.encode()
        with pytest.raises(ValueError, match='不能为空'):
            runtime_logs.append_text(db, ledger['attempt'], 2, '')
        with pytest.raises(ValueError, match='不存在'):
            runtime_logs.append_text(db, 'f' * 32, 0, '孤立分段')


def test_cursor_paging_reads_every_character_once(ledger):
    parts = ['甲' * 7 + '\n', '乙' * 5 + '\n', '丙' * 9 + '\n']
    append(ledger, parts)
    text, pages, cursor = read_all(ledger, limit=16)
    assert text == ''.join(parts) and pages >= 3
    assert cursor == len(''.join(parts).encode())
    with Session.begin() as db:
        db.get(Execution, ledger['execution']).log_status = 'COMPLETE'
    final = read_page(ledger, cursor)
    assert final['chunks'] == [] and final['finished'] is True and final['truncated'] is False
    assert final['segment_count'] == 3 and final['log_status'] == 'COMPLETE'
    # 游标跳过分段边界时按字节续读，不重复也不丢字符。
    middle = len(parts[0].encode()) + 4
    assert ''.join(chunk['text'] for chunk in read_page(ledger, middle)['chunks']) == ''.join(parts)[
        len(parts[0]) + 1:]


def test_missing_segments_are_marked_as_gaps(ledger):
    append(ledger, ['第一段\n', '第二段\n', '第三段\n'])
    with Session() as db:
        segments = runtime_logs.segments_of(db, ledger['attempt'])
        # 只丢文件、保留索引：分段仍然被引用，读取时必须显式报告缺口。
        Path(db.get(Artifact, segments[1].artifact_id).path).unlink()
    page = read_page(ledger, 0)
    text = ''.join(chunk['text'] for chunk in page['chunks'])
    assert '第一段' in text and '第三段' in text
    assert '日志分段 #1 缺失' in text and page['gap'] is True
    # 缺口不伪装成结束：日志状态没到终态时界面必须继续轮询。
    assert page['finished'] is False
    assert sum(1 for chunk in page['chunks'] if '#1' in chunk['text']) == 1


def test_deleted_first_segment_is_reported_instead_of_silently_skipped(ledger):
    append(ledger, ['旧内容\n', '新内容\n'])
    with Session() as db:
        segment = runtime_logs.segments_of(db, ledger['attempt'])[0]
        db.delete(segment)
        db.commit()
    page = read_page(ledger, 0)
    assert ''.join(chunk['text'] for chunk in page['chunks']) == '新内容\n'
    assert page['gap'] is True


def test_download_concatenates_segments_in_order(ledger):
    parts = ['开始\n', '处理中\n', '结束\n']
    append(ledger, parts)
    assert ''.join(runtime_logs.download_chunks(ledger['execution'], ledger['attempt'])) == ''.join(parts)
    with Session() as db:
        segments = runtime_logs.segments_of(db, ledger['attempt'])
        Path(db.get(Artifact, segments[2].artifact_id).path).unlink()
    assert '缺失或校验失败' in ''.join(runtime_logs.download_chunks(ledger['execution'], ledger['attempt']))
    with pytest.raises(Exception) as missing:
        list(runtime_logs.download_chunks(ledger['execution'], 'f' * 32))
    assert '运行尝试不存在' in str(missing.value)


def test_attempt_falls_back_to_the_latest_attempt_and_legacy_text(ledger):
    with Session.begin() as db:
        execution = db.get(Execution, ledger['execution'])
        execution.current_attempt_id = None
        second = ExecutionAttempt(execution_id=execution.id, number=2, log='只有短尾部的旧记录\n')
        db.add(second)
        db.flush()
        second_id = second.id
    with Session() as db:
        execution = db.get(Execution, ledger['execution'])
        assert runtime_logs.attempt_of(db, execution).id == second_id
        page = runtime_logs.page(db, execution, runtime_logs.attempt_of(db, execution))
    # 旧模拟运行没有加密分段时按单段返回，界面口径保持一致。
    assert ''.join(chunk['text'] for chunk in page['chunks']) == '只有短尾部的旧记录\n'
    assert page['segment_count'] == 0 and page['cursor'] == len('只有短尾部的旧记录\n'.encode())
    with Session.begin() as db:
        attempt = db.get(ExecutionAttempt, ledger['attempt'])
        db.delete(attempt)
    with Session() as db:
        assert runtime_logs.attempt_of(db, db.get(Execution, ledger['execution'])).id == second_id
    with Session.begin() as db:
        db.delete(db.get(ExecutionAttempt, second_id))
    with Session() as db:
        with pytest.raises(Exception) as empty:
            runtime_logs.attempt_of(db, db.get(Execution, ledger['execution']))
    assert '还没有运行尝试' in str(empty.value)


def test_segment_writer_splits_utf8_safely(ledger, monkeypatch):
    monkeypatch.setattr(settings, 'native_log_segment_bytes', 4096)
    writer = runtime_logs.SegmentWriter(ledger['execution'], ledger['attempt'])
    text = ''.join(f'第{index:06d}行：检查钱包进度\n' for index in range(2000))
    for offset in range(0, len(text), 700):
        writer.feed(text[offset:offset + 700])
    writer.flush()
    assert writer.failure == '' and writer.bytes == len(text.encode())
    assert writer.seq >= 10
    writer.flush()
    with Session() as db:
        segments = runtime_logs.segments_of(db, ledger['attempt'])
        assert len(segments) == writer.seq
        # 每个分段都是完整 UTF-8，拼接结果与原始文本逐字符一致。
        assert ''.join(runtime_logs.segment_bytes(db, segment).decode() for segment in segments) == text
    assert read_all(ledger, limit=256 * 1024)[0] == text


def test_segment_writer_stops_after_a_storage_failure(ledger, monkeypatch):
    def broken(db, attempt_id, seq, text):
        raise OSError('磁盘不可写')

    monkeypatch.setattr(runtime_logs, 'append_text', broken)
    writer = runtime_logs.SegmentWriter(ledger['execution'], ledger['attempt'])
    writer.feed('先写一段正常日志\n')
    writer.flush()
    assert '磁盘不可写' in writer.failure and writer.bytes == 0 and writer.seq == 0
    writer.feed('之后的输出不再假装保存成功\n')
    writer.flush()
    assert writer.bytes == 0
    with Session() as db:
        assert runtime_logs.segments_of(db, ledger['attempt']) == []


def test_logs_api_pages_and_downloads(logged, ledger):
    append(ledger, ['第一行\n', '第二行\n'])
    with Session.begin() as db:
        db.get(Execution, ledger['execution']).log_status = 'COMPLETE'
    first = logged.get(f"/api/executions/{ledger['execution']}/logs?limit_bytes=16").json()
    assert first['chunks'] and first['truncated'] is True and first['finished'] is False
    rest = logged.get(f"/api/executions/{ledger['execution']}/logs?cursor={first['cursor']}").json()
    assert ''.join(chunk['text'] for chunk in first['chunks'] + rest['chunks']) == '第一行\n第二行\n'
    assert rest['finished'] is True and rest['gap'] is False
    download = logged.get(f"/api/executions/{ledger['execution']}/logs/download")
    assert download.status_code == 200 and download.text == '第一行\n第二行\n'
    assert 'attachment' in download.headers['content-disposition']
    assert logged.get('/api/executions/ffff/logs').status_code == 404
    assert logged.get('/api/executions/ffff/logs/download').status_code == 404
