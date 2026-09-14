"""HASHI Context Cache (HCC): deterministic, user-controlled current context."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from orchestrator.config_json import managed_file_write_lock
from orchestrator.pcm import atomic_write_pcm, canonical_agent_md, load_pcm_document, render_pcm_document
from orchestrator.workspace_state import WorkspaceStateStore

HCC_STATE_KEY = "hcc_enabled"
HCC_SYSTEM_GUIDANCE = (
    "The following is periodically refreshed HASHI Context Cache. "
    "Use it only when relevant to the user's current request. "
    "It may not be exact real-time information; when the user clearly requires "
    "live or more precise information, use the appropriate live tool or offer to verify."
)
_ENTRY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_hcc_enabled(workspace_dir: str | Path) -> bool:
    return WorkspaceStateStore(Path(workspace_dir)).read().get(HCC_STATE_KEY) is True


def set_hcc_enabled(workspace_dir: str | Path, enabled: bool) -> bool:
    store = WorkspaceStateStore(Path(workspace_dir))
    value = bool(enabled)

    def mutate(state: dict) -> dict:
        state[HCC_STATE_KEY] = value
        return state

    store.update(mutate)
    return value


def _entry_markers(name: str) -> tuple[str, str]:
    clean = str(name or "").strip()
    if not _ENTRY_NAME.fullmatch(clean):
        raise ValueError("HCC entry name must use 1-64 letters, digits, dot, dash or underscore")
    return f"<!-- hcc:{clean} -->", f"<!-- hcc:{clean}_end -->"


def replace_hcc_entry(
    workspace_dir: str | Path,
    name: str,
    content: str,
    *,
    expected_digest: str | None = None,
    lock_timeout: float = 5.0,
) -> str:
    """Atomically replace one named HCC entry without disturbing other PCM content."""
    workspace = Path(workspace_dir)
    path = canonical_agent_md(workspace)
    start, end = _entry_markers(name)
    body = str(content or "").strip()
    if not body:
        raise ValueError("HCC entry content must not be empty")

    with managed_file_write_lock(path, lock_timeout):
        document = load_pcm_document(path, workspace_dir=workspace)
        current = document.hcc
        pattern = re.compile(
            rf"(?ms)^\s*{re.escape(start)}\s*\n(.*?)\n\s*{re.escape(end)}\s*$"
        )
        match = pattern.search(current)
        old_body = match.group(1).strip() if match else ""
        old_digest = hashlib.sha256(old_body.encode("utf-8")).hexdigest() if match else None
        if expected_digest is not None and old_digest != expected_digest:
            raise RuntimeError("HCC entry changed since inspection")
        replacement = f"{start}\n{body}\n{end}"
        if match:
            updated_hcc = current[: match.start()] + replacement + current[match.end() :]
        else:
            updated_hcc = (current.rstrip() + ("\n\n" if current.strip() else "") + replacement)
        rendered = render_pcm_document(
            persona=document.persona,
            system=document.system,
            memory=document.memory,
            hcc=updated_hcc,
        )
        atomic_write_pcm(path, rendered)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def inspect_hcc_entry(workspace_dir: str | Path, name: str) -> dict[str, object]:
    workspace = Path(workspace_dir)
    document = load_pcm_document(canonical_agent_md(workspace), workspace_dir=workspace)
    start, end = _entry_markers(name)
    pattern = re.compile(rf"(?ms)^\s*{re.escape(start)}\s*\n(.*?)\n\s*{re.escape(end)}\s*$")
    match = pattern.search(document.hcc)
    if not match:
        return {"exists": False, "digest": None}
    body = match.group(1).strip()
    return {
        "exists": True,
        "digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "chars": len(body),
    }
