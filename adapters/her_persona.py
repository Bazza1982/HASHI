"""Canonical read-only Persona source handling for HER.

HER never guesses identity from a prompt, a workspace filename, conversation
history, or an Agent catalogue.  The resolved ``system_md`` configuration value
is the sole Persona source.  This module deliberately owns no fallback filename
policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters import her_habits

MAX_PERSONA_RENDER_CHARS = 24_000
MAX_PERSONA_PACKAGING_CHARS = 12_000
PERSONA_BLOCK_BEGIN = "<!-- HASHI:PERSONA:BEGIN -->"
PERSONA_BLOCK_END = "<!-- HASHI:PERSONA:END -->"


@dataclass(frozen=True)
class HERPersonaPackagingSource:
    """The only Persona material the HER v2 commentary packager may receive."""

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
    """Read only the concrete configured ``system_md`` path as UTF-8.

    Missing, unreadable and empty sources are explicit results.  No conventional
    filename is searched and the configured file is never created or modified.
    """

    if system_md is None or not str(system_md).strip():
        return HERPersonaSource(
            path=None,
            content="",
            available=False,
            nonempty=False,
            content_sha256=None,
            unavailable_reason="system_md_not_configured",
        )

    path = Path(system_md).expanduser()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HERPersonaSource(
            path=path,
            content="",
            available=False,
            nonempty=False,
            content_sha256=None,
            unavailable_reason="system_md_missing",
        )
    except (OSError, UnicodeError) as exc:
        return HERPersonaSource(
            path=path,
            content="",
            available=False,
            nonempty=False,
            content_sha256=None,
            unavailable_reason=f"system_md_unreadable:{type(exc).__name__}",
        )

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    nonempty = bool(content.strip())
    return HERPersonaSource(
        path=path,
        content=content,
        available=True,
        nonempty=nonempty,
        content_sha256=digest,
        unavailable_reason=None if nonempty else "system_md_empty",
    )


def load_persona_packaging_source(
    system_md: str | Path | None,
    *,
    display_name: str | None,
) -> HERPersonaPackagingSource:
    """Extract one explicit Persona block without exposing the rest of agent.md.

    Missing, repeated, reversed, empty, or oversized markers select the
    deterministic minimal fallback.  Content outside the markers is never
    returned to the HER v2 commentary packaging lane.
    """

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

    begin_count = source.content.count(PERSONA_BLOCK_BEGIN)
    end_count = source.content.count(PERSONA_BLOCK_END)
    if begin_count == 0 and end_count == 0:
        reason = "persona_block_missing"
    elif begin_count != 1 or end_count != 1:
        reason = "persona_block_ambiguous"
    else:
        begin = source.content.find(PERSONA_BLOCK_BEGIN) + len(PERSONA_BLOCK_BEGIN)
        end = source.content.find(PERSONA_BLOCK_END)
        reason = "persona_block_reversed" if end < begin else ""
        if not reason:
            guidance = source.content[begin:end].strip()
            if not guidance:
                reason = "persona_block_empty"
            elif len(guidance) > MAX_PERSONA_PACKAGING_CHARS:
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
                        content_sha256=hashlib.sha256(
                            guidance.encode("utf-8")
                        ).hexdigest(),
                    )
                reason = "persona_block_empty_after_redaction"

    return HERPersonaPackagingSource(
        guidance="",
        display_name=safe_display_name,
        usable=False,
        unavailable_reason=reason,
        content_sha256=None,
    )
