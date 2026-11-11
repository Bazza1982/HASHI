"""Functions-owned lifecycle timing; diagnostic observation never changes admission.

Wall intervals may overlap across tasks/processes and must not be summed.
Detailed call observation is opt-in in state/instance/lifecycle-timing.json.
Only stage names, identities and timings are recorded, never call arguments.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

_context = contextvars.ContextVar("lifecycle_timing", default={})
_home: Path | None = None
_log_directory: Path | None = None
_write_lock = threading.Lock()
_monitor_id: int | None = None
_observed: dict = {}
_calls: dict = {}
_monitor_enabled = False
_monitor_expires_at = 0.0
_last_config_check = 0.0

# Observe selected code objects, without replacing functions or changing Core.
_OBSERVERS = {
    "orchestrator.runtime_contract": {
        "load_qualified_runtime", "verify_runtime_files", "enforce_runtime_contract",
        "current_runtime_fingerprint", "cached_current_runtime_fingerprint",
    },
    "orchestrator.kernel_artifact": {"verify_artifact"},
    "orchestrator.runtime_process": {
        "_selected_runtime", "_request_generation", "launch_function_process",
    },
    "orchestrator.runtime_fingerprint_cache": {"cached_current_runtime_fingerprint"},
    "orchestrator.runtime_inventory": {"runtime_dependency_digest"},
    "orchestrator.function_generation": {
        "probe_function_generation", "build_source_manifest", "configured_observers_are_qualified",
    },
    "orchestrator.function_worker_supervisor": {
        "load_qualified_generation_cache", "load_bootable_generation_cache",
        "materialize_generation_artifact", "persist_qualified_generation_cache",
        "verify_generation_artifact",
    },
    "orchestrator.workbench_auth": {"_windows_remote_runtime_sids"},
    "orchestrator.runtime_lifecycle": {"initialize"},
    "orchestrator.config": {"ConfigManager.load"},
    "orchestrator.flexible_agent_runtime": {"FlexibleAgentRuntime.initialize"},
    "orchestrator.startup_manager": {"StartupManager._ensure_remote_lifecycle"},
    "orchestrator.runtime_app": {"UniversalOrchestrator._load_config_bundle"},
}

def _identity(args):
    first = args[0] if args else None
    bootstrap = first if isinstance(first, dict) else getattr(first, "bootstrap", {})
    bootstrap = bootstrap or {}
    kernel = getattr(first, "kernel", first)
    paths = getattr(kernel, "paths", None)
    home = bootstrap.get("bridge_home") or getattr(first, "bridge_home", None)
    home = home or getattr(paths, "bridge_home", None)
    manifest = bootstrap.get("manifest") or {}
    if not isinstance(manifest, dict):
        manifest = {}
    other = args[1] if len(args) > 1 else None
    agent = getattr(first, "agent_name", "") or getattr(first, "name", "")
    agent = agent or (other if isinstance(other, str) else getattr(other, "agent_name", ""))
    identity = {
        "agent": str(agent or ""),
        "generation_id": str(manifest.get("generation_id") or ""),
    }
    return home, {key: value for key, value in identity.items() if value}

def emit(stage, event="point", **fields):
    global _log_directory
    if _home is None:
        return
    record = {
        "schema": 1, "stage": stage, "event": event, "epoch_ns": time.time_ns(),
        "monotonic_ns": time.perf_counter_ns(), "pid": os.getpid(),
        "parent_pid": os.getppid(), "thread_id": threading.get_ident(),
        **_context.get(), **fields,
    }
    try:
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with _write_lock:
            directory = _home / "logs" / "lifecycle"
            if _log_directory != directory:
                directory.mkdir(parents=True, exist_ok=True)
                _log_directory = directory
            with (directory / f"{os.getpid()}.jsonl").open("ab") as stream:
                stream.write(line)
    except (OSError, TypeError, ValueError):
        # Diagnostics must never make a lifecycle action fail.
        pass

def _process_birth():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if kernel.GetProcessTimes(kernel.GetCurrentProcess(), ctypes.byref(created),
                                  ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (ticks - 116444736000000000) * 100
    except Exception:
        pass
    return None

def configure(home):
    global _home, _monitor_enabled, _monitor_expires_at
    if home is not None and _home is None:
        _home = Path(home)
        birth = _process_birth()
        emit("process.first_functions_observation", process_started_epoch_ns=birth,
             pre_observation_ms=(time.time_ns() - birth) / 1e6 if birth else None)
        try:
            cfg = json.loads((_home / "state" / "instance" / "lifecycle-timing.json").read_text("utf-8"))
            _monitor_expires_at = float(cfg.get("expires_at", 0))
            _monitor_enabled = cfg.get("observe_calls") is True and _monitor_expires_at > time.time()
        except (OSError, ValueError, TypeError):
            pass
    if _monitor_enabled:
        try:
            _install_observers()
        except Exception as exc:
            _monitor_enabled = False
            emit("diagnostic.observer_failed", exception_type=type(exc).__name__)

def _call_key(code):
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task else 0, code

def _started(code, offset):
    global _last_config_check, _monitor_enabled, _monitor_id
    try:
        now = time.time()
        if now - _last_config_check >= 1:
            _last_config_check = now
            if now >= _monitor_expires_at or not (_home / "state/instance/lifecycle-timing.json").is_file():
                _monitor_enabled = False
                if _monitor_id is not None:
                    for selected_code in _observed:
                        sys.monitoring.set_local_events(_monitor_id, selected_code, 0)
                    sys.monitoring.set_events(_monitor_id, 0)
                    sys.monitoring.free_tool_id(_monitor_id)
                    _monitor_id = None
                emit("diagnostic.observer_stopped", unfinished_calls=sum(len(v) for v in _calls.values()))
                return
        key = _call_key(code)
        span = uuid.uuid4().hex
        _calls.setdefault(key, []).append((time.perf_counter_ns(), time.thread_time_ns(), span))
        emit(_observed[code], "start", span_id=span, detail=True)
    except Exception:
        pass

def _ended(code, offset, result):
    if code not in _observed:
        return
    try:
        stack = _calls.get(_call_key(code), [])
        if stack:
            wall, cpu, span = stack.pop()
            emit(_observed[code], "end", span_id=span, detail=True,
                 duration_ms=(time.perf_counter_ns() - wall) / 1e6,
                 thread_cpu_ms=(time.thread_time_ns() - cpu) / 1e6)
    except Exception:
        pass

def _install_observers():
    global _monitor_id
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return
    if _monitor_id is None:
        for candidate in (3, 4, 5):
            if monitoring.get_tool(candidate) is None:
                try:
                    monitoring.use_tool_id(candidate, "hashi-lifecycle-timing")
                    _monitor_id = candidate
                    break
                except ValueError:
                    pass
        if _monitor_id is None:
            emit("diagnostic.observer_unavailable")
            return
        monitoring.register_callback(_monitor_id, monitoring.events.PY_START, _started)
        monitoring.register_callback(_monitor_id, monitoring.events.PY_RETURN, _ended)
        monitoring.register_callback(_monitor_id, monitoring.events.PY_UNWIND, _ended)
        # CPython permits PY_UNWIND globally, not in the local event mask.
        # The callback records only code objects selected below.
        monitoring.set_events(_monitor_id, monitoring.events.PY_UNWIND)
    for module_name, names in _OBSERVERS.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for name in names:
            value = module
            for part in name.split("."):
                value = getattr(value, part, None)
            code = getattr(inspect.unwrap(value), "__code__", None)
            if code is not None and code not in _observed:
                _observed[code] = f"{module_name}.{name}"
                monitoring.set_local_events(
                    _monitor_id, code,
                    monitoring.events.PY_START | monitoring.events.PY_RETURN,
                )

@contextlib.contextmanager
def span(stage, **fields):
    identity = {**_context.get(), **fields}
    token = _context.set(identity)
    span_id = uuid.uuid4().hex
    started = time.perf_counter_ns()
    cpu = time.thread_time_ns()
    emit(stage, "start", span_id=span_id)
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        emit(stage, "end", span_id=span_id, outcome=outcome,
             duration_ms=(time.perf_counter_ns() - started) / 1e6,
             thread_cpu_ms=(time.thread_time_ns() - cpu) / 1e6)
        _context.reset(token)

def timed(stage):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                home, identity = _identity(args)
                configure(home)
                with span(stage, **identity):
                    return await function(*args, **kwargs)
            return asynchronous
        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            home, identity = _identity(args)
            configure(home)
            with span(stage, **identity):
                return function(*args, **kwargs)
        return synchronous
    return decorate
# HASHI-Fingerprint: hfp_2c1a3352d5364a1aa4f91bafc9de5381
