from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

AUTOMATION_SCRIPTS = {
    "agent-audit": Path("skills/agent-audit/scripts/agent_audit.py"),
    "hermes-memory-import": Path("skills/hermes-memory-import/scripts/hermes_memory_import.py"),
    "memory-consolidation": Path("skills/memory-consolidation/scripts/memory_consolidation.py"),
    "remote-guard": Path("skills/remote-guard/scripts/remote_guard.py"),
}
LEGACY_AUTOMATION_SCRIPTS = {
    "remote-guard": Path("skills/remote_guard/remote_guard.py"),
}
_AUTOMATION_LOCKS: dict[str, asyncio.Lock] = {}


def canonical_automation_id(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def is_automation(value: str) -> bool:
    return canonical_automation_id(value) in AUTOMATION_SCRIPTS


async def run_automation(
    *,
    project_root: Path,
    workspace_dir: Path,
    automation_id: str,
    args: str = "",
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run one Jobs-owned deterministic automation with legacy ID aliases."""

    canonical_id = canonical_automation_id(automation_id)
    canonical_path = AUTOMATION_SCRIPTS.get(canonical_id)
    if canonical_path is None:
        return False, f"Unknown automation: {automation_id}"

    relative_candidates = [canonical_path]
    legacy_path = LEGACY_AUTOMATION_SCRIPTS.get(canonical_id)
    if legacy_path is not None:
        relative_candidates.append(legacy_path)
    run_path = next(
        (
            candidate
            for relative_path in relative_candidates
            if (candidate := project_root / relative_path).is_file()
        ),
        project_root / canonical_path,
    )
    if not run_path.is_file():
        return False, f"Automation target not found: {run_path}"

    suffix = run_path.suffix.lower()
    if suffix == ".py":
        cmd = [sys.executable, str(run_path)]
    elif suffix == ".ps1":
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(run_path)]
    elif suffix == ".bat":
        cmd = ["cmd", "/c", str(run_path)]
    else:
        cmd = [str(run_path)]
    if args.strip():
        # Preserve the historical action contract: the scheduler passes the
        # configured argument payload as one positional value.
        cmd.append(args.strip())

    resolved_workspace = workspace_dir.resolve()
    lock_key = f"{resolved_workspace}::{canonical_id}"
    lock = _AUTOMATION_LOCKS.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        return False, f"Automation '{canonical_id}' is already running."

    env = os.environ.copy()
    env["BRIDGE_PROJECT_ROOT"] = str(project_root)
    env["BRIDGE_WORKSPACE_DIR"] = str(workspace_dir)
    env["BRIDGE_AUTOMATION_ID"] = canonical_id
    # Temporary environment compatibility for local scripts that previously
    # inspected the action-Skill identifier.
    env["BRIDGE_SKILL_ID"] = canonical_id.replace("-", "_")
    if extra_env:
        env.update({key: str(value) for key, value in extra_env.items() if value is not None})

    proc: asyncio.subprocess.Process | None = None
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            if proc is not None:
                with suppress(Exception):
                    proc.kill()
                with suppress(Exception):
                    await proc.wait()
            raise
        except OSError as exc:
            return False, f"Automation '{canonical_id}' failed to start: {exc}"

    out_text = stdout.decode("utf-8", errors="replace").strip()
    err_text = stderr.decode("utf-8", errors="replace").strip()
    lines: list[str] = []
    if out_text:
        lines.append(out_text)
    if err_text:
        lines.append(f"stderr:\n{err_text}")
    if proc.returncode != 0:
        lines.append(f"exit_code={proc.returncode}")
    text = "\n\n".join(lines).strip() or f"Automation '{canonical_id}' completed."
    return proc.returncode == 0, text
