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
import re
import shlex
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


_SHELL_CONTROL_TOKENS = re.compile(r'[;&|><`\r\n]|\$\(')
_SHELL_ESCAPE_TOKENS = re.compile(
    r'(^|[\s"\'])\.\.([\\/]|$|[\s"\'])|(^|[\s"\'])~([\\/]|$)|\$env:|%[A-Za-z_][A-Za-z0-9_]*%|\b(?:cd|chdir|pushd|set-location|push-location|sl)\b',
    re.IGNORECASE,
)
_READONLY_SHELL_COMMANDS = frozenset({
    'rg', 'ripgrep', 'grep', 'findstr', 'where', 'where.exe', 'which',
    'pwd', 'ls', 'dir', 'get-childitem', 'get-location', 'get-content',
    'select-string', 'test-path', 'stat', 'wc', 'head', 'tail', 'cat',
})
_READONLY_GIT_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'branch', 'rev-parse', 'remote',
    'ls-files', 'describe', 'tag',
})
_MUTATING_GIT_TOKENS = frozenset({
    '-d', '-D', '-m', '-M', '-c', '-C', '-f', '--delete', '--force',
    '--move', '--copy', 'add', 'remove', 'rm', 'rename', 'set-url',
    'prune', 'update',
})


def _is_strictly_readonly_shell_command(command: str) -> bool:
    """Return True only for single, unchained, provably read-only commands."""
    text = str(command or '').strip()
    if not text or _SHELL_CONTROL_TOKENS.search(text):
        return False
    try:
        tokens = shlex.split(text, posix=(os.name != 'nt'))
    except ValueError:
        return False
    if not tokens:
        return False
    exe = Path(tokens[0].strip('"\'')).name.casefold()
    if exe.endswith('.exe'):
        exe_base = exe[:-4]
    else:
        exe_base = exe
    if exe in _READONLY_SHELL_COMMANDS or exe_base in _READONLY_SHELL_COMMANDS:
        return True
    if exe_base == 'git':
        idx = 1
        while idx < len(tokens):
            tok = tokens[idx]
            if tok in {'-C', '--git-dir', '--work-tree'}:
                idx += 2
                continue
            if tok.startswith(('--git-dir=', '--work-tree=', '--no-pager', '--paginate')):
                idx += 1
                continue
            if tok.startswith('-'):
                return False
            sub = tok.casefold()
            if sub not in _READONLY_GIT_SUBCOMMANDS:
                return False
            rest = {t.strip('"\'') for t in tokens[idx + 1:]}
            if rest & _MUTATING_GIT_TOKENS:
                return False
            # `git branch <new-branch>` or `git tag <new-tag>` creates refs.
            if sub in {'branch', 'tag'}:
                positionals = [t for t in tokens[idx + 1:] if not t.startswith('-')]
                if positionals:
                    return False
            return True
    return False


def _resolve_tool_target(raw_value: object, workspace_dir: Path) -> Path | None:
    text = str(raw_value or '').strip()
    if not text:
        return None
    raw_path = Path(text)
    return raw_path.resolve() if raw_path.is_absolute() else (workspace_dir / raw_path).resolve()


def _shell_lock_targets(registry, arguments: dict, roots: list[str]) -> list[Path]:
    """Scope shell leases to the execution directory and any referenced roots."""
    workspace = Path(registry.workspace_dir).resolve()
    raw_cwd = str(arguments.get('cwd') or '').strip()
    exec_dir = _resolve_tool_target(raw_cwd, workspace) if raw_cwd else workspace
    if exec_dir is None:
        exec_dir = workspace
    command = str(arguments.get('command') or '').strip()
    if not command or _SHELL_ESCAPE_TOKENS.search(command):
        return [Path(p) for p in roots]

    norm_cmd = command.casefold().replace('\\', '/')
    targets: list[Path] = [exec_dir]
    for raw_root in roots:
        root_path = Path(raw_root)
        if root_path == exec_dir:
            continue
        # If the execution dir is an ancestor of another root, locking exec_dir
        # already covers it; if another root is explicitly referenced in the
        # command, include it so cross-root writes remain synchronized.
        root_norm = str(root_path).casefold().replace('\\', '/')
        if root_norm and root_norm in norm_cmd:
            targets.append(root_path)
    return targets


@asynccontextmanager
async def tool_resource(registry, name, arguments):
    """Synchronize mutating filesystem tools without over-locking independent work.

    Known file mutations (`file_write`, `text_replace`, `apply_patch`,
    `file_manage`) take shared parent-directory leases plus exclusive target
    file leases, so independent files proceed concurrently while same-file
    edits serialize across tools and processes. Strictly read-only shell
    inspections take shared leases; arbitrary/mutating shell commands take
    exclusive leases scoped to their execution directory and referenced roots.
    Reads take no lease.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    if name not in {'shell', 'bash', 'file_write', 'apply_patch', 'text_replace', 'file_manage'}:
        yield 0
        return
    if name == 'text_replace' and arguments.get('preview_only') is True:
        yield 0
        return
    context = registry._effective_audit_context()
    config = context.get('global_config')
    root = getattr(config, 'bridge_home', None) or getattr(config, 'project_root', None) or registry.workspace_dir
    workspace_dir = Path(registry.workspace_dir).resolve()
    roots = sorted({str(Path(p).resolve()) for p in [workspace_dir, *registry.access_roots]})
    scopes: dict[str, bool] = {}

    def scope(path: Path, shared: bool):
        key = 'filesystem:' + os.path.normcase(str(path))
        scopes[key] = scopes.get(key, True) and shared

    def lock_path(path: Path, *, shared: bool = False):
        for parent in path.parents:
            scope(parent, True)
        scope(path, shared)

    if name in {'file_write', 'text_replace', 'apply_patch'}:
        target = _resolve_tool_target(arguments.get('path'), workspace_dir)
        if target is not None:
            lock_path(target, shared=False)
        else:
            for raw_scope in roots:
                lock_path(Path(raw_scope), shared=False)
    elif name == 'file_manage':
        op = str(arguments.get('operation') or '').strip().casefold()
        locked_any = False
        if op == 'mkdir':
            target = _resolve_tool_target(arguments.get('path'), workspace_dir)
            if target is not None:
                lock_path(target, shared=False)
                locked_any = True
        elif op in {'copy', 'move'}:
            src = _resolve_tool_target(arguments.get('source'), workspace_dir)
            dst = _resolve_tool_target(arguments.get('destination'), workspace_dir)
            if src is not None:
                lock_path(src, shared=(op == 'copy'))
                locked_any = True
            if dst is not None:
                lock_path(dst, shared=False)
                locked_any = True
        if not locked_any:
            for raw_scope in roots:
                lock_path(Path(raw_scope), shared=False)
    else:
        command = str(arguments.get('command') or '')
        readonly_shell = _is_strictly_readonly_shell_command(command)
        for target_root in _shell_lock_targets(registry, arguments, roots):
            lock_path(target_root, shared=readonly_shell)

    groups = [(key, 1, shared) for key, shared in scopes.items()]
    async with leases(Path(root) / 'state' / 'instance', groups) as elapsed:
        yield elapsed
