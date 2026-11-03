"""Revision-checked external editing of the canonical Agent PCM document."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from orchestrator.pcm import (
    PCMValidationError,
    canonical_agent_md,
    load_pcm_document,
    update_pcm_text,
)

MAX_AGENT_PCM_BYTES = 2 * 1024 * 1024


def read_agent_pcm(workspace_dir: str | Path) -> dict[str, str]:
    workspace = Path(workspace_dir).expanduser().resolve()
    path = canonical_agent_md(workspace)
    if path.is_symlink():
        raise PCMValidationError("pcm_symlink_forbidden", "canonical document is not a regular file")
    try:
        if path.stat().st_size > MAX_AGENT_PCM_BYTES:
            raise PCMValidationError("pcm_too_large", "canonical document exceeds the editing limit")
        document = load_pcm_document(path, workspace_dir=workspace)
        raw = path.read_bytes()
    except OSError as exc:
        raise PCMValidationError("pcm_unreadable", "canonical document cannot be read") from exc
    if len(raw) > MAX_AGENT_PCM_BYTES or path.is_symlink():
        raise PCMValidationError("pcm_conflict", "canonical document changed during read")
    if hashlib.sha256(raw).hexdigest() != document.content_sha256:
        raise PCMValidationError("pcm_conflict", "canonical document changed during read")
    return {"content": raw.decode("utf-8"), "revision": document.content_sha256}


def replace_agent_pcm(workspace_dir: str | Path, expected_revision: str, content: str) -> dict[str, str]:
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_revision):
        raise PCMValidationError("pcm_revision_invalid", "document revision is invalid")
    if not isinstance(content, str):
        raise PCMValidationError("pcm_not_text", "document must be text")
    try:
        if len(content.encode("utf-8")) > MAX_AGENT_PCM_BYTES:
            raise PCMValidationError("pcm_too_large", "document exceeds the editing limit")
    except UnicodeEncodeError as exc:
        raise PCMValidationError("pcm_invalid_utf8", "document must be valid UTF-8") from exc

    def replace(current: str) -> str:
        if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_revision:
            raise PCMValidationError("pcm_conflict", "document changed since it was opened")
        return content

    saved = update_pcm_text(workspace_dir, replace)
    return {"revision": saved.content_sha256}
