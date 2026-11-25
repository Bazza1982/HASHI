from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from orchestrator.kernel_process import instance_runtime_dir, canonical_instance_home


HOME_PREFIX = "@home/"


@dataclass(frozen=True)
class BridgePaths:
    code_root: Path
    bridge_home: Path
    instance_id: str
    config_path: Path
    secrets_path: Path
    tasks_path: Path
    state_path: Path
    lock_path: Path
    pid_path: Path
    workspaces_root: Path


def expand_path_string(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value.strip()))


def resolve_bridge_home(code_root: Path, override: str | Path | None = None) -> Path:
    return canonical_instance_home(code_root, override)


def resolve_home_file(
    bridge_home: Path,
    code_root: Path,
    filename: str,
    validator: Optional[Callable[[Path], bool]] = None,
) -> Path:
    home_path = bridge_home / filename
    legacy_path = code_root / filename
    if home_path.exists():
        if validator is None or validator(home_path):
            return home_path
        # File exists but content is invalid — fall through to legacy
    if bridge_home == code_root or not legacy_path.exists():
        return home_path
    return legacy_path


# ── Validators for resolve_home_file ──────────────────────────────

PLACEHOLDER_TOKENS = frozenset({"WORKBENCH_ONLY_NO_TOKEN", ""})


def _secrets_validator(path: Path) -> bool:
    """Return True if secrets.json contains at least one real token."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or not data:
        return False
    # Check that at least one agent-level token is real
    skip_keys = {"authorized_telegram_id", "openrouter_key"}
    for key, value in data.items():
        if key in skip_keys:
            continue
        if isinstance(value, str) and value.strip() not in PLACEHOLDER_TOKENS:
            return True
    return False


def _json_validator(path: Path) -> bool:
    """Return True if the file is valid, non-empty JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    if isinstance(data, dict):
        return bool(data)
    return data is not None


def resolve_instance_id(config_path: Path) -> str:
    """Read the configured instance identity without loading runtime secrets."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return "HASHI"
    global_config = raw.get("global") if isinstance(raw, dict) else None
    if not isinstance(global_config, dict):
        return "HASHI"
    instance_id = str(global_config.get("instance_id") or "").strip()
    return instance_id or "HASHI"



def build_bridge_paths(code_root: Path, bridge_home: str | Path | None = None, *, canonical_home: bool = False) -> BridgePaths:
    resolved_code_root = code_root.resolve()
    resolved_home = Path(bridge_home).resolve() if canonical_home else resolve_bridge_home(resolved_code_root, bridge_home)
    config_path = resolve_home_file(
        resolved_home,
        resolved_code_root,
        "agents.json",
        validator=_json_validator,
    )
    instance_id = resolve_instance_id(config_path)
    runtime_dir = instance_runtime_dir(resolved_home)
    return BridgePaths(
        code_root=resolved_code_root,
        bridge_home=resolved_home,
        instance_id=instance_id,
        config_path=config_path,
        secrets_path=resolve_home_file(resolved_home, resolved_code_root, "secrets.json", validator=_secrets_validator),
        tasks_path=resolve_home_file(resolved_home, resolved_code_root, "tasks.json"),
        state_path=resolved_home / "scheduler_state.json",
        lock_path=runtime_dir / "process.lock",
        pid_path=runtime_dir / "process.pid",
        workspaces_root=resolved_home / "workspaces",
    )


def resolve_path_value(
    value: str | Path | None,
    *,
    config_dir: Path,
    bridge_home: Path,
) -> Path | None:
    if value is None:
        return None
    raw = expand_path_string(str(value))
    if not raw:
        return None
    normalized = raw.replace("\\", "/")
    if normalized.startswith(HOME_PREFIX):
        suffix = normalized[len(HOME_PREFIX):]
        return (bridge_home / Path(suffix)).resolve()
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate.resolve()
    return (config_dir / candidate).resolve()


def resolve_command_value(
    value: str | None,
    *,
    config_dir: Path,
    bridge_home: Path,
) -> str:
    if value is None:
        return ""
    raw = expand_path_string(str(value))
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    looks_like_path = normalized.startswith(HOME_PREFIX) or "/" in raw or "\\" in raw or Path(raw).is_absolute()
    if looks_like_path:
        resolved = resolve_path_value(raw, config_dir=config_dir, bridge_home=bridge_home)
        return str(resolved) if resolved is not None else ""
    # On Windows, asyncio.create_subprocess_exec won't find .cmd/.ps1 wrappers.
    # Use shutil.which() to resolve bare command names to their full path.
    if os.name == "nt":
        full = shutil.which(raw)
        if full:
            return full
    return raw


def resolve_agy_executable(value: str | None) -> str:
    """Resolve the ``agy`` command for the Antigravity CLI backend.

    Candidates, in order:

    1. an explicitly configured value (a filesystem path wins when it exists),
    2. ``%LOCALAPPDATA%\agy\bin\agy.exe`` (official installer location),
    3. the bare command name ``agy`` on PATH.
    """

    raw = str(value or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "agy" / "bin" / "agy.exe"))
    candidates.append("agy")
    for candidate in candidates:
        if not candidate:
            continue
        if ("/" in candidate or "\\" in candidate or Path(candidate).is_absolute()) and Path(
            candidate
        ).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return raw or "agy"


def resolve_dsh_invocation(
    value: str | None = None,
    *,
    profile: str = "acp",
    patches: tuple[str, ...] = (),
) -> tuple[list[str], Path | None, str]:
    """Resolve the DeepSeek Harness (``dsh``) command into ``(argv, cwd, label)``.

    Candidates, in order:

    1. An explicitly configured command or script path (or ``HASHI_DSH_CMD``),
    2. ``dsh`` on ``PATH``,
    3. Local source/built installation under ``~/projects/deepseek-harness``
       (preferring precompiled ``apps/cli/lib/bin.js``, falling back to
       ``apps/cli/src/bin.ts`` via ``tsx/esm``).
    """

    raw = str(value or os.environ.get("HASHI_DSH_CMD") or "").strip()
    node_exe = shutil.which("node") or "node"
    extra_args: list[str] = ["--profile", str(profile or "acp").strip() or "acp"]
    for patch in patches:
        patch_str = str(patch or "").strip()
        if patch_str:
            extra_args.extend(["--patch", patch_str])

    def _from_script(script: Path) -> tuple[list[str], Path | None, str]:
        resolved = script.resolve()
        # apps/cli/{lib|src}/bin.{js|ts} -> repo root is 3 parents up
        repo_root = (
            resolved.parents[3]
            if len(resolved.parents) >= 4
            and resolved.parent.name in {"lib", "src"}
            and resolved.parents[1].name == "cli"
            and resolved.parents[2].name == "apps"
            else resolved.parent
        )
        suffix = resolved.suffix.lower()
        if suffix in {".ts", ".mts"}:
            argv = [node_exe, "--import", "tsx/esm", str(resolved), *extra_args]
        else:
            argv = [node_exe, str(resolved), *extra_args]
        return argv, repo_root, str(resolved)

    def _check_candidate(candidate: str) -> tuple[list[str], Path | None, str] | None:
        if not candidate:
            return None
        path_obj = Path(candidate)
        if ("/" in candidate or "\\" in candidate or path_obj.is_absolute()) and path_obj.is_file():
            if path_obj.suffix.lower() in {".js", ".mjs", ".cjs", ".ts", ".mts"}:
                return _from_script(path_obj)
            return [str(path_obj.resolve()), *extra_args], None, str(path_obj.resolve())
        found = shutil.which(candidate)
        if found:
            return [found, *extra_args], None, found
        return None

    # 1. Explicit configured value (ignore bare default "dsh" if we also want local fallback)
    if raw and raw.lower() != "dsh":
        resolved = _check_candidate(raw)
        if resolved is not None:
            return resolved
        # Allow multi-token command strings such as "node /path/to/bin.js"
        import shlex

        tokens = [
            tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
            for tok in shlex.split(raw, posix=(os.name != "nt"))
        ]
        if len(tokens) > 1:
            exe = shutil.which(tokens[0]) or tokens[0]
            return [exe, *tokens[1:], *extra_args], None, raw

    # 2. Bare "dsh" on PATH
    found_dsh = shutil.which("dsh")
    if found_dsh:
        return [found_dsh, *extra_args], None, found_dsh

    # 3. Local DeepSeek Harness checkout under home/projects
    home_repo = Path.home() / "projects" / "deepseek-harness"
    for rel in (
        Path("apps") / "cli" / "lib" / "bin.js",
        Path("apps") / "cli" / "src" / "bin.ts",
    ):
        script = home_repo / rel
        if script.is_file():
            return _from_script(script)

    return [raw or "dsh", *extra_args], None, raw or "dsh"


def resolve_dsh_executable(value: str | None = None) -> str:
    """Return a display/preflight path for ``dsh`` when installed locally."""

    argv, _cwd, label = resolve_dsh_invocation(value)
    if label and Path(label).is_file():
        return label
    if argv and shutil.which(argv[0]):
        return label or argv[0]
    return str(value or "dsh").strip() or "dsh"


def to_home_relative(path: str | Path, *, bridge_home: Path) -> str:
    candidate = Path(path).resolve()
    try:
        rel = candidate.relative_to(bridge_home.resolve())
    except ValueError:
        return str(candidate)
    return HOME_PREFIX + rel.as_posix()
