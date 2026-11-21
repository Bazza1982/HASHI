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


_SHELL_METACHARS = re.compile(r'[;&|><`\r\n]|\$\(|\$\{|@\(|@\{|%[A-Za-z_][A-Za-z0-9_]*%')
_UNQUOTED_DOLLAR = re.compile(r'\$')
_GIT_SHORT_COUNT = re.compile(r'^-\d+$')
_GIT_UNIFIED_SHORT = re.compile(r'^-U\d+$')
_RG_CONTEXT_SHORT = re.compile(r'^-[ABCm]\d+$')
_PS_INVOKE_PREFIXES = frozenset({
    'iex', 'invoke-expression', 'invoke-command', 'icm', 'start-process',
    'saps', 'powershell', 'pwsh', 'cmd', 'bash', 'sh', 'wsl', 'python',
    'python3', 'py', 'pytest', 'node', 'npm', 'npx', 'uv',
})

# Strict, option-aware allowlists for read-only shell classification.
# Any unrecognized flag or dangerous option (e.g. `git diff --output`,
# `git -c`, `rg --pre`, pipes, redirects, chaining) returns False so the
# command falls back to the conservative mutating-shell lease path.
_GIT_GLOBAL_VALUE_FLAGS = frozenset({'-C', '--git-dir', '--work-tree'})
_GIT_GLOBAL_BOOL_FLAGS = frozenset({
    '--no-pager', '--paginate', '--bare', '--literal-pathspecs',
    '--glob-pathspecs', '--noglob-pathspecs', '--icase-pathspecs',
})
_GIT_GLOBAL_PREFIX_FLAGS = ('--git-dir=', '--work-tree=')

_GIT_STATUS_BOOL_FLAGS = frozenset({
    '-s', '--short', '-b', '--branch', '--show-stash', '--porcelain',
    '--porcelain=v1', '--porcelain=v2', '--long', '-v', '--verbose',
    '-u', '-uall', '-uno', '-unormal', '--untracked-files', '--ignored',
    '--ignore-submodules', '--column', '--no-column', '--ahead-behind',
    '--no-ahead-behind', '--renames', '--no-renames', '-z',
})
_GIT_STATUS_PREFIX_FLAGS = ('--untracked-files=', '--ignored=', '--ignore-submodules=')

_GIT_DIFF_BOOL_FLAGS = frozenset({
    '--stat', '--numstat', '--shortstat', '--summary', '--name-only',
    '--name-status', '--cached', '--staged', '--patch', '-p', '-u',
    '--no-patch', '-s', '--raw', '--check', '--color', '--no-color',
    '--word-diff', '--ignore-space-change', '-b', '--ignore-all-space',
    '-w', '--ignore-blank-lines', '--no-ext-diff', '--no-textconv',
    '--no-renames', '-M', '--find-renames', '-C', '--find-copies',
    '--relative', '--exit-code', '--quiet', '-q',
})
_GIT_DIFF_PREFIX_FLAGS = ('--unified=', '--color=', '--word-diff=', '--relative=', '--find-renames=', '--find-copies=')

_GIT_LOG_BOOL_FLAGS = frozenset({
    '--oneline', '--stat', '--numstat', '--shortstat', '--name-only',
    '--name-status', '--graph', '--decorate', '--no-decorate', '--all',
    '--branches', '--tags', '--remotes', '--first-parent', '--merges',
    '--no-merges', '--reverse', '--abbrev-commit', '-p', '--patch',
    '--no-patch', '-s', '--color', '--no-color', '--no-ext-diff',
    '--no-textconv', '--full-history', '--simplify-by-decoration',
})
_GIT_LOG_VALUE_FLAGS = frozenset({'-n', '--max-count', '--format', '--pretty', '--grep', '--author', '--since', '--until', '--after', '--before'})
_GIT_LOG_PREFIX_FLAGS = ('--max-count=', '--format=', '--pretty=', '--grep=', '--author=', '--since=', '--until=', '--after=', '--before=', '--color=', '--decorate=', '-n')

_GIT_SHOW_BOOL_FLAGS = frozenset({
    '--stat', '--numstat', '--shortstat', '--name-only', '--name-status',
    '--oneline', '--no-patch', '-s', '-p', '--patch', '--color',
    '--no-color', '--no-ext-diff', '--no-textconv', '--raw',
})
_GIT_SHOW_VALUE_FLAGS = frozenset({'--format', '--pretty'})
_GIT_SHOW_PREFIX_FLAGS = ('--format=', '--pretty=', '--color=')

_GIT_REV_PARSE_BOOL_FLAGS = frozenset({
    '--short', '--abbrev-ref', '--show-toplevel', '--show-prefix',
    '--git-dir', '--git-common-dir', '--is-inside-work-tree',
    '--is-bare-repository', '--verify', '--quiet', '-q',
    '--symbolic-full-name', '--show-cdup',
})
_GIT_REV_PARSE_PREFIX_FLAGS = ('--short=', '--abbrev-ref=')

_GIT_BRANCH_BOOL_FLAGS = frozenset({
    '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose',
    '--list', '--show-current', '--merged', '--no-merged',
    '--color', '--no-color',
})
_GIT_BRANCH_VALUE_FLAGS = frozenset({'--contains', '--no-contains', '--points-at', '--sort', '--format'})
_GIT_BRANCH_PREFIX_FLAGS = ('--contains=', '--no-contains=', '--points-at=', '--sort=', '--format=', '--color=')

_GIT_TAG_BOOL_FLAGS = frozenset({'-l', '--list', '-n', '--merged', '--no-merged', '--color', '--no-color'})
_GIT_TAG_VALUE_FLAGS = frozenset({'--sort', '--contains', '--no-contains', '--points-at', '--format'})
_GIT_TAG_PREFIX_FLAGS = ('--sort=', '--contains=', '--no-contains=', '--points-at=', '--format=', '--color=', '-n')

_GIT_LS_FILES_BOOL_FLAGS = frozenset({
    '-c', '--cached', '-m', '--modified', '-o', '--others', '-d',
    '--deleted', '-i', '--ignored', '--exclude-standard', '-s',
    '--stage', '-u', '--unmerged', '-z', '--full-name',
})
_GIT_GREP_BOOL_FLAGS = frozenset({
    '-n', '--line-number', '-i', '--ignore-case', '-F', '--fixed-strings',
    '-w', '--word-regexp', '-E', '--extended-regexp', '-P', '--perl-regexp',
    '-l', '--files-with-matches', '--name-only', '-c', '--count',
    '-v', '--invert-match', '-H', '--heading', '--break', '--cached',
    '-I', '-q', '--quiet', '--color', '--no-color', '-z', '--null',
    '--untracked', '--no-index', '--exclude-standard', '--full-name',
})
_GIT_GREP_VALUE_FLAGS = frozenset({
    '-e', '-A', '--after-context', '-B', '--before-context', '-C', '--context',
    '--max-count', '--max-depth', '--threads', '--color',
})
_GIT_GREP_PREFIX_FLAGS = (
    '--max-count=', '--max-depth=', '--threads=', '--after-context=',
    '--before-context=', '--context=', '--color=', '-A', '-B', '-C',
)
_GIT_BLAME_BOOL_FLAGS = frozenset({
    '-n', '-l', '-t', '-e', '--show-email', '-p', '--porcelain',
    '--line-porcelain', '-w', '-M', '-C', '-s', '--show-name',
    '--show-number', '--incremental', '-c', '--root', '--date',
})
_GIT_BLAME_VALUE_FLAGS = frozenset({'-L', '--abbrev', '--since', '--date', '-M', '-C'})
_GIT_BLAME_PREFIX_FLAGS = ('-L', '--abbrev=', '--since=', '--date=')
_GIT_LS_TREE_BOOL_FLAGS = frozenset({
    '-r', '-d', '-t', '-l', '--long', '--name-only', '--name-status',
    '--full-name', '--full-tree', '-z',
})
_GIT_LS_TREE_VALUE_FLAGS = frozenset({'--abbrev'})
_GIT_LS_TREE_PREFIX_FLAGS = ('--abbrev=',)
_GIT_DESCRIBE_BOOL_FLAGS = frozenset({
    '--tags', '--all', '--always', '--dirty', '--broken', '--long',
    '--exact-match', '--contains', '--first-parent',
})
_GIT_DESCRIBE_VALUE_FLAGS = frozenset({'--abbrev', '--candidates', '--match', '--exclude'})
_GIT_DESCRIBE_PREFIX_FLAGS = ('--abbrev=', '--candidates=', '--match=', '--exclude=')
_GIT_SHORTLOG_BOOL_FLAGS = frozenset({
    '-s', '--summary', '-n', '--numbered', '-sn', '-ns', '-e', '--email', '-c',
    '--committer', '--all', '--branches', '--tags', '--no-merges', '-z',
})
_GIT_SHORTLOG_VALUE_FLAGS = frozenset({'-n', '--max-count', '--since', '--until', '--format'})
_GIT_SHORTLOG_PREFIX_FLAGS = ('--max-count=', '--since=', '--until=', '--format=', '-n')
_GIT_CAT_FILE_BOOL_FLAGS = frozenset({'-p', '-t', '-s', '-e'})

_RG_BOOL_FLAGS = frozenset({
    '-n', '--line-number', '-N', '--no-line-number', '-i', '--ignore-case',
    '-s', '--case-sensitive', '-S', '--smart-case', '-F', '--fixed-strings',
    '-w', '--word-regexp', '-l', '--files-with-matches', '--files-without-match',
    '--files', '-c', '--count', '--count-matches', '-v', '--invert-match',
    '-o', '--only-matching', '-H', '--with-filename', '--no-filename', '-I',
    '-q', '--quiet', '--hidden', '--no-ignore', '-u', '-uu', '-uuu',
    '--follow', '-L', '--no-heading', '--heading', '--vimgrep', '--json',
    '--stats', '--color', '-0', '--null',
})
_RG_VALUE_FLAGS = frozenset({
    '-g', '--glob', '-t', '--type', '-T', '--type-not', '-A', '--after-context',
    '-B', '--before-context', '-C', '--context', '-m', '--max-count',
    '--max-depth', '-e', '--regexp', '--color',
})
_RG_PREFIX_FLAGS = (
    '--glob=', '--type=', '--type-not=', '--after-context=', '--before-context=',
    '--context=', '--max-count=', '--max-depth=', '--regexp=', '--color=',
)

_LS_BOOL_FLAGS = frozenset({'-l', '-a', '-la', '-al', '-1', '-R', '-h', '-lh', '-la1', '-A', '/b', '/a', '/s', '/w', '/o', '/od', '/on'})
_PS_GCI_BOOL_FLAGS = frozenset({'-name', '-force', '-recurse', '-file', '-directory'})
_PS_GCI_VALUE_FLAGS = frozenset({'-path', '-literalpath', '-filter', '-depth', '-include', '-exclude'})
_PS_GC_BOOL_FLAGS = frozenset({'-raw'})
_PS_GC_VALUE_FLAGS = frozenset({'-path', '-literalpath', '-totalcount', '-first', '-head', '-tail', '-encoding'})
_PS_SS_BOOL_FLAGS = frozenset({'-simplematch', '-casesensitive', '-list', '-quiet', '-allmatches'})
_PS_SS_VALUE_FLAGS = frozenset({'-pattern', '-path', '-literalpath', '-context', '-encoding'})
_PS_TP_VALUE_FLAGS = frozenset({'-path', '-literalpath', '-pathtype'})
_HEAD_TAIL_VALUE_FLAGS = frozenset({'-n', '--lines'})
_HEAD_TAIL_PREFIX_FLAGS = ('--lines=', '-n')
_WC_BOOL_FLAGS = frozenset({'-l', '-w', '-c', '-m', '-L'})
_FINDSTR_BOOL_FLAGS = frozenset({'/s', '/i', '/n', '/r', '/c', '/l', '/m', '/v', '/x', '/b', '/e', '/p', '/o'})


def _tokenize_readonly_shell(text: str) -> list[tuple[str, bool]] | None:
    """Conservatively tokenize a single shell command preserving Windows paths.

    Returns a list of ``(effective_token, was_quoted)`` tuples, or ``None`` if
    the command contains any chaining, redirection, substitution, backtick
    escape, backslash-escaped quote (which parses differently across
    PowerShell 5.1, Bash, and Win32 argv), unbalanced quote, or ambiguous
    PowerShell syntax.

    Note: ``was_quoted`` is tracked ONLY so a quoted ``'--'`` cannot act as an
    option terminator; quoted flags (e.g. ``'--output=file'``) are ALWAYS
    validated against the command's real option grammar.
    """
    if not text or _SHELL_METACHARS.search(text):
        return None
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_quote: str | None = None
    was_quoted = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote is not None:
            if ch == '\\' and i + 1 < n and text[i + 1] in {'"', "'"}:
                # Backslash-escaped quotes parse inconsistently across
                # PowerShell 5.1, Bash, and Win32 argv; classify conservatively.
                return None
            if ch == in_quote:
                in_quote = None
            elif in_quote == '"' and ch == '$':
                # PowerShell expands $variables inside double quotes.
                return None
            else:
                buf.append(ch)
        else:
            if ch == '\\' and i + 1 < n and text[i + 1] in {'"', "'"}:
                return None
            if ch in {'"', "'"}:
                in_quote = ch
                was_quoted = True
            elif ch.isspace():
                if buf or was_quoted:
                    tokens.append((''.join(buf), was_quoted))
                    buf.clear()
                    was_quoted = False
            elif ch == '$':
                # Unquoted $var / $env: in PowerShell or bash.
                return None
            else:
                buf.append(ch)
        i += 1
    if in_quote is not None:
        return None
    if buf or was_quoted:
        tokens.append((''.join(buf), was_quoted))
    return tokens or None


def _validate_flag_stream(
    args: list[tuple[str, bool]],
    *,
    bool_flags: frozenset[str] = frozenset(),
    value_flags: frozenset[str] = frozenset(),
    prefix_flags: tuple[str, ...] = (),
    short_patterns: tuple[re.Pattern[str], ...] = (),
    case_insensitive_flags: bool = False,
    slash_flags: frozenset[str] = frozenset(),
) -> list[str] | None:
    """Validate every effective argument against explicit option allowlists.

    Quotation marks group shell arguments but never exempt an option from
    grammar validation: quoted flags such as ``'--output=file'`` or
    ``'--pre=python'`` are validated identically to unquoted flags. Only a
    genuine unquoted ``--`` option terminator switches subsequent tokens to
    positional-only parsing.
    """
    positionals: list[str] = []
    after_double_dash = False
    i = 0
    while i < len(args):
        tok, quoted = args[i]
        if after_double_dash:
            positionals.append(tok)
            i += 1
            continue
        if tok == '--' and not quoted:
            after_double_dash = True
            i += 1
            continue
        check = tok.casefold() if case_insensitive_flags else tok
        if slash_flags and tok.startswith('/') and len(tok) <= 4:
            if tok.casefold() in slash_flags:
                i += 1
                continue
            return None
        if tok.startswith('-') and tok != '-':
            if check in bool_flags:
                i += 1
                continue
            if check in value_flags:
                if i + 1 >= len(args):
                    return None
                i += 2
                continue
            if any(check.startswith(p) and len(check) > len(p) for p in prefix_flags):
                i += 1
                continue
            if any(pat.match(tok) for pat in short_patterns):
                i += 1
                continue
            return None
        positionals.append(tok)
        i += 1
    return positionals


def _validate_readonly_git(args: list[tuple[str, bool]]) -> bool:
    idx = 0
    while idx < len(args):
        tok, _quoted = args[idx]
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            if idx + 1 >= len(args):
                return False
            idx += 2
            continue
        if tok in _GIT_GLOBAL_BOOL_FLAGS:
            idx += 1
            continue
        if any(tok.startswith(p) and len(tok) > len(p) for p in _GIT_GLOBAL_PREFIX_FLAGS):
            idx += 1
            continue
        if tok.startswith('-'):
            # Explicitly rejects `-c`, `--config-env`, quoted `'-c'`, and unknown global flags.
            return False
        sub = tok.casefold()
        rest = args[idx + 1:]
        if sub == 'status':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_STATUS_BOOL_FLAGS,
                prefix_flags=_GIT_STATUS_PREFIX_FLAGS,
            ) is not None
        if sub == 'diff':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_DIFF_BOOL_FLAGS,
                prefix_flags=_GIT_DIFF_PREFIX_FLAGS,
                short_patterns=(_GIT_UNIFIED_SHORT,),
            ) is not None
        if sub == 'log':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_LOG_BOOL_FLAGS,
                value_flags=_GIT_LOG_VALUE_FLAGS,
                prefix_flags=_GIT_LOG_PREFIX_FLAGS,
                short_patterns=(_GIT_SHORT_COUNT,),
            ) is not None
        if sub == 'show':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_SHOW_BOOL_FLAGS,
                value_flags=_GIT_SHOW_VALUE_FLAGS,
                prefix_flags=_GIT_SHOW_PREFIX_FLAGS,
            ) is not None
        if sub == 'rev-parse':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_REV_PARSE_BOOL_FLAGS,
                prefix_flags=_GIT_REV_PARSE_PREFIX_FLAGS,
            ) is not None
        if sub == 'branch':
            pos = _validate_flag_stream(
                rest,
                bool_flags=_GIT_BRANCH_BOOL_FLAGS,
                value_flags=_GIT_BRANCH_VALUE_FLAGS,
                prefix_flags=_GIT_BRANCH_PREFIX_FLAGS,
            )
            if pos is None:
                return False
            has_list = any(t == '--list' for t, _q in rest)
            return has_list or len(pos) == 0
        if sub == 'tag':
            pos = _validate_flag_stream(
                rest,
                bool_flags=_GIT_TAG_BOOL_FLAGS,
                value_flags=_GIT_TAG_VALUE_FLAGS,
                prefix_flags=_GIT_TAG_PREFIX_FLAGS,
            )
            if pos is None:
                return False
            has_list = any(t in {'-l', '--list'} for t, _q in rest)
            return has_list or len(pos) == 0
        if sub == 'remote':
            if not rest:
                return True
            if len(rest) == 1 and rest[0][0] in {'-v', '--verbose'}:
                return True
            if (
                len(rest) == 2
                and rest[0][0].casefold() in {'show', 'get-url'}
                and not rest[1][0].startswith('-')
            ):
                return True
            return False
        if sub == 'ls-files':
            return _validate_flag_stream(rest, bool_flags=_GIT_LS_FILES_BOOL_FLAGS) is not None
        if sub == 'grep':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_GREP_BOOL_FLAGS,
                value_flags=_GIT_GREP_VALUE_FLAGS,
                prefix_flags=_GIT_GREP_PREFIX_FLAGS,
                short_patterns=(_RG_CONTEXT_SHORT,),
            ) is not None
        if sub == 'blame':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_BLAME_BOOL_FLAGS,
                value_flags=_GIT_BLAME_VALUE_FLAGS,
                prefix_flags=_GIT_BLAME_PREFIX_FLAGS,
            ) is not None
        if sub == 'ls-tree':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_LS_TREE_BOOL_FLAGS,
                value_flags=_GIT_LS_TREE_VALUE_FLAGS,
                prefix_flags=_GIT_LS_TREE_PREFIX_FLAGS,
            ) is not None
        if sub == 'describe':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_DESCRIBE_BOOL_FLAGS,
                value_flags=_GIT_DESCRIBE_VALUE_FLAGS,
                prefix_flags=_GIT_DESCRIBE_PREFIX_FLAGS,
            ) is not None
        if sub == 'shortlog':
            return _validate_flag_stream(
                rest,
                bool_flags=_GIT_SHORTLOG_BOOL_FLAGS,
                value_flags=_GIT_SHORTLOG_VALUE_FLAGS,
                prefix_flags=_GIT_SHORTLOG_PREFIX_FLAGS,
                short_patterns=(_GIT_SHORT_COUNT,),
            ) is not None
        if sub == 'cat-file':
            pos = _validate_flag_stream(rest, bool_flags=_GIT_CAT_FILE_BOOL_FLAGS)
            return pos is not None and len(pos) in (1, 2)
        if sub == 'reflog':
            if rest and rest[0][0].casefold() in {'expire', 'delete', 'exists'}:
                if rest[0][0].casefold() != 'exists':
                    return False
            reflog_rest = rest[1:] if rest and rest[0][0].casefold() == 'show' else rest
            return _validate_flag_stream(
                reflog_rest,
                bool_flags=_GIT_LOG_BOOL_FLAGS,
                value_flags=_GIT_LOG_VALUE_FLAGS,
                prefix_flags=_GIT_LOG_PREFIX_FLAGS,
                short_patterns=(_GIT_SHORT_COUNT,),
            ) is not None
        return False
    return False


def _is_strictly_readonly_shell_command(command: str) -> bool:
    """Return True only when the entire command and all its options are provably read-only."""
    text = str(command or '').strip()
    tokens = _tokenize_readonly_shell(text)
    if not tokens:
        return False
    raw_exe, _ = tokens[0]
    if not raw_exe or raw_exe in {'.', '&'}:
        return False
    exe = Path(raw_exe).name.casefold()
    exe_base = exe[:-4] if exe.endswith('.exe') else exe
    if exe_base in _PS_INVOKE_PREFIXES:
        return False
    rest = tokens[1:]

    if exe_base == 'git':
        return _validate_readonly_git(rest)
    if exe_base in {'rg', 'ripgrep'}:
        return _validate_flag_stream(
            rest,
            bool_flags=_RG_BOOL_FLAGS,
            value_flags=_RG_VALUE_FLAGS,
            prefix_flags=_RG_PREFIX_FLAGS,
            short_patterns=(_RG_CONTEXT_SHORT,),
        ) is not None
    if exe_base in {'pwd', 'get-location'}:
        return len(rest) == 0
    if exe_base in {'ls', 'dir'}:
        return _validate_flag_stream(
            rest,
            bool_flags=_LS_BOOL_FLAGS,
            slash_flags=_LS_BOOL_FLAGS,
        ) is not None
    if exe_base == 'get-childitem':
        return _validate_flag_stream(
            rest,
            bool_flags=_PS_GCI_BOOL_FLAGS,
            value_flags=_PS_GCI_VALUE_FLAGS,
            case_insensitive_flags=True,
        ) is not None
    if exe_base == 'get-content':
        return _validate_flag_stream(
            rest,
            bool_flags=_PS_GC_BOOL_FLAGS,
            value_flags=_PS_GC_VALUE_FLAGS,
            case_insensitive_flags=True,
        ) is not None
    if exe_base == 'select-string':
        return _validate_flag_stream(
            rest,
            bool_flags=_PS_SS_BOOL_FLAGS,
            value_flags=_PS_SS_VALUE_FLAGS,
            case_insensitive_flags=True,
        ) is not None
    if exe_base == 'test-path':
        return _validate_flag_stream(
            rest,
            value_flags=_PS_TP_VALUE_FLAGS,
            case_insensitive_flags=True,
        ) is not None
    if exe_base in {'head', 'tail'}:
        return _validate_flag_stream(
            rest,
            value_flags=_HEAD_TAIL_VALUE_FLAGS,
            prefix_flags=_HEAD_TAIL_PREFIX_FLAGS,
            short_patterns=(_GIT_SHORT_COUNT,),
        ) is not None
    if exe_base == 'wc':
        return _validate_flag_stream(rest, bool_flags=_WC_BOOL_FLAGS) is not None
    if exe_base == 'cat':
        return _validate_flag_stream(rest, bool_flags=frozenset({'-n', '-b'})) is not None
    if exe_base in {'where', 'which'}:
        return _validate_flag_stream(rest, bool_flags=frozenset({'-a'})) is not None
    if exe_base == 'findstr':
        return _validate_flag_stream(rest, slash_flags=_FINDSTR_BOOL_FLAGS) is not None
    return False


def _resolve_tool_target(raw_value: object, workspace_dir: Path) -> Path | None:
    text = str(raw_value or '').strip()
    if not text:
        return None
    raw_path = Path(text)
    return raw_path.resolve() if raw_path.is_absolute() else (workspace_dir / raw_path).resolve()


def _is_filesystem_root(path: Path) -> bool:
    """Return True when ``path`` is a drive or filesystem root (e.g. ``C:\\`` or ``/``)."""
    resolved = path.resolve()
    return resolved == resolved.parent


def _shell_lock_targets(registry, arguments: dict, roots: list[str]) -> list[Path]:
    """Conservative lock targets for shell commands across accessible Workzones.

    Arbitrary scripts can mutate any accessible writable Workzone even when
    its literal path does not appear in the command string, so unknown/mutating
    shell commands lock all non-root authorized Workzones (plus workspace/cwd).
    Drive/filesystem roots added by ``access_scope='drive'`` (e.g. ``C:\\``)
    must never be exclusively locked by a single workspace shell call, or one
    Agent running tests would stall every unrelated Agent on the machine.
    """
    workspace = Path(registry.workspace_dir).resolve()
    raw_cwd = str(arguments.get('cwd') or '').strip()
    exec_dir = _resolve_tool_target(raw_cwd, workspace) if raw_cwd else workspace
    seen: set[str] = set()
    targets: list[Path] = []
    candidates = [*(Path(p) for p in roots), *([exec_dir] if exec_dir is not None else [])]
    for candidate in candidates:
        if _is_filesystem_root(candidate):
            continue
        norm = os.path.normcase(str(candidate))
        if norm not in seen:
            seen.add(norm)
            targets.append(candidate)
    if not targets:
        targets.append(workspace)
    return targets


def _shell_redirect_targets(command: str, exec_dir: Path, shell: str = "") -> list[Path]:
    """Resolve literal output-redirection targets; this is not a shell parser.

    An opaque script may write other paths. These cooperative leases cover only
    the explicit targets we can identify and do not replace OS authorization.
    """
    try:
        lexer = shlex.shlex(
            command,
            posix=(str(shell).casefold() == "bash" or os.name != "nt"),
            punctuation_chars="<>|&;",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    targets = []
    for index, token in enumerate(tokens[:-1]):
        if token not in {">", ">>"}:
            continue
        value = tokens[index + 1]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # Expansions, descriptors and globbing are not stable file identities.
        if (not value or value.startswith("~")
                or value in {"&", "|", ";", "<", ">", ">>"}
                or any(char in value for char in "$%*?[]{}()`")):
            continue
        target = _resolve_tool_target(value, exec_dir)
        if target is not None and not _is_filesystem_root(target):
            targets.append(target)
    return targets


@asynccontextmanager
async def tool_resource(registry, name, arguments):
    """Synchronize mutating filesystem tools without over-locking independent work.

    - Known file mutations (`file_write`, `text_replace`, `apply_patch`) take
      shared parent-directory leases plus exclusive target-file leases.
    - `file_manage` locks exact destinations/subtrees:
      * `mkdir`: exclusive destination directory.
      * single-file `copy`: shared source file, exclusive resolved destination
        file (deriving `destination / source.name` when destination is an
        existing directory).
      * recursive directory `copy`: exclusive source and destination subtrees
        so concurrent child-file edits cannot interleave with tree copying.
      * `move`: exclusive source and destination (file or directory subtree).
    - Provably read-only `shell`/`bash` commands take shared leases.
    - Arbitrary/mutating `shell`/`bash` commands take exclusive leases across
      accessible non-root Workzones and literal output-redirection targets.
      Opaque scripts may also write paths that cannot be derived here.
    - Pure read tools take no lease.
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
            target = _resolve_tool_target(arguments.get('destination'), workspace_dir)
            if target is not None:
                lock_path(target, shared=False)
                locked_any = True
        elif op == 'copy':
            src = _resolve_tool_target(arguments.get('source'), workspace_dir)
            dst = _resolve_tool_target(arguments.get('destination'), workspace_dir)
            recursive = arguments.get('recursive') is True
            src_is_dir = bool(src is not None and src.exists() and src.is_dir())
            if src is not None and dst is not None:
                if recursive or src_is_dir:
                    # Recursive directory copy must conflict with concurrent
                    # writes to child files inside source or destination.
                    lock_path(src, shared=False)
                    lock_path(dst, shared=False)
                else:
                    actual_dst = (dst / src.name).resolve() if (dst.exists() and dst.is_dir()) else dst
                    lock_path(src, shared=True)
                    lock_path(actual_dst, shared=False)
                locked_any = True
        elif op == 'move':
            src = _resolve_tool_target(arguments.get('source'), workspace_dir)
            dst = _resolve_tool_target(arguments.get('destination'), workspace_dir)
            if src is not None and dst is not None:
                lock_path(src, shared=False)
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
        if not readonly_shell:
            raw_cwd = str(arguments.get('cwd') or '').strip()
            exec_dir = _resolve_tool_target(raw_cwd, workspace_dir) if raw_cwd else workspace_dir
            if exec_dir is not None:
                for target in _shell_redirect_targets(command, exec_dir, str(arguments.get('shell') or '')):
                    lock_path(target, shared=False)

    groups = [(key, 1, shared) for key, shared in scopes.items()]
    async with leases(Path(root) / 'state' / 'instance', groups) as elapsed:
        yield elapsed
