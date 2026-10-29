"""Foreground sync with a soft network deadline and uninterrupted local cleanup."""
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


TIMEOUT_SECONDS = 30
DRAIN_SECONDS = 1
DEADLINE_ENV = 'AI_KNOWLEDGE_SYNC_NETWORK_DEADLINE'
SCRIPT = Path(__file__).resolve().with_name('hot_sync.py')
_deadline_clock = None
# Gate execution until the Windows job has been assigned. This prevents a fast
# child from spawning descendants before process-tree ownership is established.
GATE = ('import json,subprocess,sys; command=json.loads(sys.argv[1]); '
        'sys.stdin.buffer.read(1); '
        'raise SystemExit(subprocess.call(command,stdin=subprocess.DEVNULL))')


def _windows_job(process):
    """Return an owned kill-on-close job, or fail before releasing the child."""
    from ctypes import wintypes

    class Limits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                    ('maximum_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Limits), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                    ('peak_job', ctypes.c_size_t)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise OSError('cannot create sync process job')
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not api.AssignProcessToJobObject(job, int(process._handle))):
        api.CloseHandle(job)
        raise OSError('cannot contain sync process')
    return lambda: api.CloseHandle(job)


def _stop(process, close_job=None):
    if close_job is not None:
        close_job()  # Windows closes the entire tree, including detached children.
    elif os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        # The child is still waiting on the gate if assigning its job failed.
        process.kill()


def _failure(status):
    return {'ok':False, 'attempted':True, 'pending':True, 'status':status}


def run_network(arguments, timeout, **kwargs):
    """Contain only a remote command, never the worker performing local writes."""
    global _deadline_clock
    raw_deadline = os.environ[DEADLINE_ENV]
    if _deadline_clock is None or _deadline_clock[0] != raw_deadline:
        # Python 3.9 on macOS does not promise the same monotonic origin in
        # different processes. Transfer wall time, then use a local monotonic
        # clock for subsequent calls; a clock adjustment cannot extend waits.
        remaining = max(0, min(TIMEOUT_SECONDS, float(raw_deadline) - time.time()))
        _deadline_clock = (raw_deadline, time.monotonic() + remaining)
    remaining = _deadline_clock[1] - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(arguments, 0)
    limit = min(timeout, remaining)
    process, close_job = None, None
    try:
        command = [sys.executable, '-c', GATE, json.dumps(arguments)] if os.name == 'nt' else arguments
        process = subprocess.Popen(command, **kwargs,
            stdin=subprocess.PIPE if os.name == 'nt' else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if os.name == 'nt':
            close_job = _windows_job(process)
        stdout, stderr = process.communicate(input='\n' if os.name == 'nt' else None, timeout=limit)
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    finally:
        if process is not None:
            _stop(process, close_job)
            try:
                process.communicate(timeout=DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                # A trusted host SSH wrapper can detach itself from our POSIX
                # group and retain these pipes. Bound collection as well as the
                # network command; do not claim that such a helper was stopped.
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                process.wait(timeout=DRAIN_SECONDS)


def run_sync(root, timeout=TIMEOUT_SECONDS, script=SCRIPT):
    """Wait for safe local completion; only remote requests obey the deadline.

    Local commit, rebase, rollback and index work are not killed when the network
    deadline expires. A network failure is pending, not a promise Git needs no
    inspection. Child output never overrides evidence that a fact was saved.
    """
    environment = dict(os.environ, AI_KNOWLEDGE_ROOT=str(root),
                       PYTHONDONTWRITEBYTECODE='1', AI_KNOWLEDGE_REFRESH_DASHBOARD='0',
                       **{DEADLINE_ENV:str(time.time() + timeout)})
    try:
        process = subprocess.run([sys.executable, str(script)], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False)
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError):
            return _failure('sync-result-invalid')
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            return _failure('sync-result-invalid')
        # Child errors can contain a credential helper's diagnostic or a URL.
        # Expose only known, bounded machine statuses; preserve the successful
        # nested result only when the entire operation claims success.
        if process.returncode or result.get('ok') is not True:
            remote = result.get('sync') if isinstance(result.get('sync'), dict) else {}
            needs_recovery = (remote.get('retryable') is False or remote.get('cleanup_verified') is False
                              or remote.get('error') in {'rebase-cleanup-failed', 'push-race-rebase-cleanup-failed',
                                                        'rebase-state-present'})
            return dict(_failure('sync-recovery-required' if needs_recovery else
                                 'network-deadline' if result.get('error') == 'network-deadline' else 'sync-failed'),
                        recovery_required=needs_recovery)
        if result.get('pending') or result.get('skipped'):
            return _failure('sync-busy')
        remote = result.get('sync')
        if (not isinstance(remote, dict) or remote.get('ok') is not True
                or remote.get('pushed') is not True or remote.get('skipped')):
            return _failure('sync-unconfirmed')
        commit = result.get('commit')
        return {'ok':True, 'attempted':True, 'pending':False, 'status':'synchronized',
                'sync':{'ok':True, 'pushed':True},
                'commit':commit if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40,64}', commit) else None}
    except (OSError, ValueError, subprocess.SubprocessError):
        return _failure('sync-process-unavailable')
