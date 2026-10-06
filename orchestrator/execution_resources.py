"""Function-owned, process-safe execution quotas and filesystem tool leases.

OS file locks expire with their handles/processes. No stale TTL can release a
still-running action, and no configuration or Core lock object is copied.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ctypes
import hashlib
import inspect
import os
from pathlib import Path
import time

if os.name == 'nt':
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]

    _lock_file = ctypes.WinDLL('kernel32', use_last_error=True).LockFileEx
    _lock_file.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _lock_file.restype = wintypes.BOOL


def limit(value, name, default, maximum=64):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


class FileLease:
    def __init__(self, root, key, *, shared=False):
        self.path = Path(root) / 'resource-leases' / (hashlib.sha256(str(key).encode()).hexdigest()+'.lock')
        self.shared = shared
        self.handle = None
        self.overlapped = None

    def try_acquire(self):
        if self.handle is not None:
            raise RuntimeError('resource lease already acquired')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                overlapped = _Overlapped()
                if not _lock_file(msvcrt.get_osfhandle(handle.fileno()),1 | (0 if self.shared else 2),0,1,0,ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    handle.close()
                    if error == 33:  # ERROR_LOCK_VIOLATION: another live owner.
                        return False
                    raise ctypes.WinError(error)
                self.overlapped = overlapped
            else:
                import fcntl
                fcntl.flock(handle.fileno(),(fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            self.handle = handle
            return True
        except BlockingIOError:
            handle.close()
            return False
        except BaseException:
            handle.close()
            raise

    def close(self):
        if self.handle is not None:
            # Closing this handle releases its lock on both supported platforms.
            self.handle.close()
            self.handle = None
            self.overlapped = None


@asynccontextmanager
async def leases(root, groups, *, on_wait=None, timeout=600):
    """Acquire every group without retaining partial slots while waiting.

    A group is (resource, capacity, shared). Capacity > 1 is a semaphore;
    shared single-slot groups are reader leases paired with exclusive actions.
    Sorting makes all participants use the same order; a failed pass releases
    every partial handle before waiting, avoiding nested quota deadlock.
    """
    acquired = []
    started = time.monotonic()
    waiting = None
    try:
        while True:
            failed = None
            for key, capacity, shared in sorted(groups):
                selected = None
                for slot in range(capacity):
                    candidate = FileLease(root, f'{key}:{slot}', shared=shared)
                    if candidate.try_acquire():
                        selected = candidate
                        break
                if selected is None:
                    failed = key
                    break
                acquired.append(selected)
            if failed is None:
                break
            for handle in reversed(acquired):
                handle.close()
            acquired.clear()
            if on_wait is not None and waiting != failed:
                result = on_wait(failed)
                if inspect.isawaitable(result):
                    await result
                waiting = failed
            if time.monotonic()-started >= timeout:
                raise TimeoutError('execution_resource_wait_timeout')
            await asyncio.sleep(0.05)
        if on_wait is not None:
            result = on_wait(None)
            if inspect.isawaitable(result):
                await result
        yield round((time.monotonic()-started)*1000,1)
    finally:
        for handle in reversed(acquired):
            handle.close()


def instance_state(runtime):
    config = runtime.global_config
    root = getattr(config,'bridge_home',None) or getattr(config,'project_root',None) or runtime.config.project_root
    return Path(root) / 'state' / 'instance'


def execution_configuration(settings, extra):
    """Validate resource opt-ins before advertising a ready Worker."""
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        raise ValueError('execution settings must be an object')
    raw = settings.get('execution_limits', {})
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('execution_limits must be an object')
    engines = raw.get('engines', {})
    engines = {} if engines is None else engines
    if not isinstance(engines, dict):
        raise ValueError('execution_limits.engines must be an object')
    limit((extra or {}).get('max_concurrent_sessions'), 'max_concurrent_sessions', 2, 8)
    limit(raw.get('instance_sessions'), 'instance_sessions', 8)
    for engine, value in engines.items():
        limit(value, 'execution_limits.engines.'+str(engine), 4)
    return raw


@asynccontextmanager
async def execution_budget(runtime, item, frozen):
    raw = execution_configuration(frozen['settings'].get('_agents_json_global'), frozen['config'].extra)
    engine = str(frozen['config'].active_backend)
    engines = raw.get('engines') or {}
    groups = [('instance-sessions',limit(raw.get('instance_sessions'),'instance_sessions',8),False),
              ('session-engine:'+engine,limit(engines.get(engine),'execution_limits.engines.'+engine,4),False)]
    reasons = runtime.__dict__.setdefault('_execution_queue_reasons',{})
    async def waiting(reason):
        if reason is None:
            reasons.pop(item.request_id,None)
        else:
            reasons[item.request_id] = 'instance_capacity' if reason == 'instance-sessions' else 'engine_capacity'
        from orchestrator.runtime_lifecycle import _publish_worker_metadata
        await _publish_worker_metadata(runtime, transition='Execution capacity changed')
    try:
        async with leases(instance_state(runtime),groups,on_wait=waiting) as elapsed:
            yield elapsed
    finally:
        reasons.pop(item.request_id,None)


@asynccontextmanager
async def tool_resource(registry, name, arguments):
    """Serialize unknown shell writes against their authorized workspace set.

    Known file writes take shared workspace leases plus an exclusive file
    lease, so independent files still proceed together. Reads take no lease.
    Patch and arbitrary shell actions take exclusive workspace leases.
    """
    if name not in {'shell','bash','file_write','apply_patch'}:
        yield 0
        return
    context = registry._effective_audit_context()
    config = context.get('global_config')
    root = getattr(config,'bridge_home',None) or getattr(config,'project_root',None) or registry.workspace_dir
    roots = sorted({str(Path(p).resolve()) for p in [registry.workspace_dir,*registry.access_roots]})
    scopes = {}
    def scope(path, shared):
        key = 'filesystem:'+os.path.normcase(str(path))
        scopes[key] = scopes.get(key, True) and shared
    if name == 'file_write':
        raw_path = Path(str(arguments.get('path') or ''))
        path = raw_path.resolve() if raw_path.is_absolute() else (registry.workspace_dir/raw_path).resolve()
        for parent in path.parents:
            scope(parent, True)
        scope(path, False)
    else:
        for raw_scope in roots:
            path = Path(raw_scope)
            for parent in path.parents:
                scope(parent, True)
            scope(path, False)
    groups = [(key, 1, shared) for key, shared in scopes.items()]
    async with leases(Path(root)/'state'/'instance',groups) as elapsed:
        yield elapsed
