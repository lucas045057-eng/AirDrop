"""Limits for trusted AST tooling, separate from the untrusted-code Runner."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_RESULT = 1_500_000


def linux_limits(memory_mib, cpu_seconds):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (memory_mib * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (80 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def windows_job(pid, memory_mib):
    """Attach before feeding input. Closing the handle terminates this child only."""
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', wintypes.DWORD),
                    ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ['read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes']]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.OpenProcess.restype = wintypes.HANDLE
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    process = api.OpenProcess(0x0100 | 0x0001, False, pid)
    if not handle or not process:
        raise RuntimeError('无法建立分析子进程资源隔离')
    settings = Extended()
    settings.basic.flags = 0x2000 | 0x0100 | 0x0200 | 0x0008
    # Windows venv's redirector starts the real interpreter as a second process.
    settings.basic.active = 2
    settings.process_memory = memory_mib * 1024**2
    settings.job_memory = memory_mib * 1024**2
    try:
        if not api.SetInformationJobObject(handle, 9, ctypes.byref(settings), ctypes.sizeof(settings)) or not api.AssignProcessToJobObject(handle, process):
            api.CloseHandle(handle)
            raise RuntimeError('无法设置分析子进程资源限制')
    finally:
        api.CloseHandle(process)
    return lambda: api.CloseHandle(handle)


def own_peak_rss():
    if sys.platform == 'linux':
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if os.name == 'nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                        ['peak', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile']]
        counters = Counters(); counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32'); kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi'); psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.peak
    return None


async def run_child(operation, arguments=None, data=b'', memory_mib=256, timeout=60, cancelled=None, stage_callback=None):
    if operation not in {'upload', 'scan', 'context', 'transform'}:
        raise ValueError('未知的受控分析操作')
    if not 32 <= memory_mib <= 512 or not 1 <= timeout <= 180:
        raise ValueError('分析资源上限无效')
    command = [sys.executable, '-m', 'worker.adapter_child', operation, json.dumps(arguments or {}, ensure_ascii=False)]
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONIOENCODING='utf-8', AIRDROP_CHILD_MEMORY_MIB=str(memory_mib))
    kwargs = {'start_new_session': True} if sys.platform == 'linux' else ({'creationflags': 0x08000000} if os.name == 'nt' else {})
    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    close_job = None
    try:
        if os.name == 'nt':
            close_job = windows_job(process.pid, memory_mib)
        async def feed():
            try:
                process.stdin.write(data); await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
        async def collect(stream, limit, stages=False):
            parts, size, initial = [], 0, b''
            reading_events = stages
            while part := await stream.read(8192):
                size += len(part)
                if size > limit:
                    raise ValueError('分析子进程输出超过预算')
                if reading_events:
                    initial += part
                    while not initial.startswith(b'{') and b'\n' in initial:
                        line, initial = initial.split(b'\n', 1)
                        if not line.startswith(b'ADAPTER_STAGE:'):
                            raise ValueError('分析阶段事件无效')
                        stage = line.split(b':', 1)[1].decode().strip()
                        if stage not in {'SCANNING', 'DISCOVERING', 'GENERATING', 'VALIDATING'}:
                            raise ValueError('分析阶段事件无效')
                        if stage_callback:
                            await stage_callback(stage)
                    if not initial.startswith(b'{'):
                        continue
                    if initial:
                        parts.append(initial); initial = b''; reading_events = False
                else:
                    parts.append(part)
            return b''.join(parts)
        feeding = asyncio.create_task(feed())
        stdout = asyncio.create_task(collect(process.stdout, MAX_RESULT, True))
        stderr = asyncio.create_task(collect(process.stderr, 8192))
        while process.returncode is None:
            if cancelled and await cancelled():
                raise asyncio.CancelledError('分析任务已取消')
            if time.monotonic() - start > timeout:
                raise TimeoutError('分析超过处理时限，原版本保留')
            for task in (stdout, stderr):
                if task.done() and task.exception():
                    raise task.exception()
            await asyncio.sleep(.05)
        output = await stdout
        error_bytes = await stderr; await feeding
        if process.returncode:
            import re
            kinds = re.findall(rb'^([A-Za-z]+(?:Error|Exception)):', error_bytes, re.M)
            kind = kinds[-1].decode() if kinds else 'RESOURCE_LIMIT'
            raise ValueError(f'分析子进程被资源限制或异常终止（{kind}, {process.returncode}）；原版本保留，请缩小源码后重试')
        try:
            envelope = json.loads(output)
        except ValueError:
            raise ValueError('分析子进程没有返回有效结果') from None
        if not envelope.get('ok'):
            raise ValueError(envelope.get('error', '静态处理失败'))
        return envelope['result'], {**envelope.get('resources', {}), 'wall_seconds': round(time.monotonic() - start, 3),
                                    'memory_limit_mib': memory_mib, 'timeout_seconds': timeout, 'platform': sys.platform}
    finally:
        if process.returncode is None:
            if sys.platform == 'linux':
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else:
                process.kill()
            await process.wait()
        for name in ('feeding', 'stdout', 'stderr'):
            task = locals().get(name)
            if task is not None and not task.done():
                task.cancel()
        local_tasks = locals()
        cleanup_tasks = [local_tasks.get(name) for name in ('feeding', 'stdout', 'stderr')]
        await asyncio.gather(*(task for task in cleanup_tasks if task is not None), return_exceptions=True)
        if close_job:
            close_job()
