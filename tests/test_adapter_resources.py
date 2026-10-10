import asyncio
import pytest
from worker.process_limits import run_child


async def test_upload_and_static_scan_in_bounded_child(logged):
    result, metrics = await run_child('upload', {'filename': 'safe.py'}, b'def main(): return 1\nif __name__=="__main__": main()\n', timeout=30)
    assert result['analysis']['python_count'] == 1
    report, scan_metrics = await run_child('scan', {'source_path': result['sources']['path'], 'source_sha256': result['sources']['sha256']}, timeout=30)
    assert len(report['features']) == 1
    assert metrics['uploaded_code_executed'] is False
    assert metrics['peak_rss_bytes'] and scan_metrics['peak_rss_bytes']
    assert logged.get('/api/health').status_code == 200


async def test_child_memory_failure_keeps_api_healthy_and_original_data(logged):
    with pytest.raises(ValueError, match='资源|预算'):
        await run_child('upload', {'filename': 'large.py'}, b'x=1\n' * 300_000, memory_mib=32, timeout=10)
    assert logged.get('/api/health').status_code == 200
    assert logged.get('/api/scripts').json() == []


async def test_cancellation_only_terminates_owned_child(logged):
    async def cancelled(): return True
    with pytest.raises(asyncio.CancelledError):
        await run_child('upload', {'filename': 'safe.py'}, b'pass\n', cancelled=cancelled)
    assert logged.get('/api/health').status_code == 200


async def test_bad_archive_rejection_keeps_api_alive(logged):
    from tests.test_uploads import zipped
    with pytest.raises(ValueError, match='路径'):
        await run_child('upload', {'filename': 'hostile.zip'}, zipped({'../bad.py': b'pass'}))
    assert logged.get('/api/health').status_code == 200
