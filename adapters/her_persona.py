"""Canonical read-only Persona source handling for HER.

HER reads the Persona block from the exact lower-case workspace ``agent.md``.
Strict PCM validation belongs to HASHI; presentation fallbacks remain an
internal defensive lane and never make an invalid Agent configuration valid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters import her_habits
from orchestrator.pcm import PCMValidationError, load_pcm_document

MAX_PERSONA_RENDER_CHARS = 24_000
MAX_PERSONA_PACKAGING_CHARS = 12_000
PERSONA_BLOCK_BEGIN = "[persona]"
PERSONA_BLOCK_END = "[persona_end]"


@dataclass(frozen=True)
class HERPersonaPackagingSource:
    """The only Persona material an HER v2 presentation lane may receive."""

    guidance: str
    display_name: str
    usable: bool
    unavailable_reason: str | None
    content_sha256: str | None

    def audit_fields(self) -> dict[str, Any]:
        return {
            "persona_packaging_block_available": self.usable,
            "persona_packaging_block_sha256": self.content_sha256,
            "persona_packaging_block_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class HERPersonaSource:
    """One exact configured Persona source and its safe diagnostics."""

    path: Path | None
    content: str
    available: bool
    nonempty: bool
    content_sha256: str | None
    unavailable_reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.available and self.nonempty

    def model_guidance(self, *, limit: int = MAX_PERSONA_RENDER_CHARS) -> str:
        """Return bounded, secret-redacted guidance without replacing its source."""

        if not self.usable:
            return ""
        return her_habits.redact_bounded_text(self.content, limit=max(1, int(limit)))

    def audit_fields(self) -> dict[str, Any]:
        """Return diagnostics without logging the private Persona contents."""

        if self.path is None:
            path_label = None
            path_sha256 = None
        else:
            try:
                normalized = self.path.expanduser().resolve(strict=False).as_posix()
            except OSError:
                normalized = self.path.expanduser().absolute().as_posix()
            path_label = self.path.name
            path_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return {
            "persona_source_configured": self.path is not None,
            "persona_source_label": path_label,
            "persona_source_path_sha256": path_sha256,
            "persona_source_available": self.available,
            "persona_source_nonempty": self.nonempty,
            "persona_source_content_sha256": self.content_sha256,
            "persona_source_reason": self.unavailable_reason,
        }


def load_configured_persona(system_md: str | Path | None) -> HERPersonaSource:
    """Read only the Persona block of canonical, strictly valid PCM."""

    if system_md is None or not str(system_md).strip():
        return HERPersonaSource(
            path=None,
            content="",
            available=False,
            nonempty=False,
            content_sha256=None,
            unavailable_reason="pcm_not_configured",
        )

    path = Path(system_md).expanduser()
    try:
        document = load_pcm_document(path, workspace_dir=path.parent)
    except PCMValidationError as exc:
        return HERPersonaSource(
            path=path,
            content="",
            available=False,
            nonempty=False,
            content_sha256=None,
            unavailable_reason=exc.code,
        )

    content = document.persona
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return HERPersonaSource(
        path=path,
        content=content,
        available=True,
        nonempty=True,
        content_sha256=digest,
        unavailable_reason=None,
    )


def load_persona_packaging_source(
    system_md: str | Path | None,
    *,
    display_name: str | None,
) -> HERPersonaPackagingSource:
    """Package the already-isolated Persona block for a presentation lane."""

    source = load_configured_persona(system_md)
    safe_display_name = str(display_name or "").strip()[:120] or "HASHI"
    if not source.usable:
        return HERPersonaPackagingSource(
            guidance="",
            display_name=safe_display_name,
            usable=False,
            unavailable_reason=source.unavailable_reason,
            content_sha256=None,
        )

    guidance = source.content.strip()
    if len(guidance) > MAX_PERSONA_PACKAGING_CHARS:
        reason = "persona_block_oversized"
    else:
        guidance = her_habits.redact_bounded_text(
            guidance,
            limit=MAX_PERSONA_PACKAGING_CHARS,
        ).strip()
        if guidance:
            return HERPersonaPackagingSource(
                guidance=guidance,
                display_name=safe_display_name,
                usable=True,
                unavailable_reason=None,
                content_sha256=hashlib.sha256(guidance.encode("utf-8")).hexdigest(),
            )
        reason = "persona_block_empty_after_redaction"

    return HERPersonaPackagingSource(
        guidance="",
        display_name=safe_display_name,
        usable=False,
        unavailable_reason=reason,
        content_sha256=None,
    )
