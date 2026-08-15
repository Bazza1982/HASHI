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
