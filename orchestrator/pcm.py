"""Strict Persona-Context-Memory document parsing and legacy conversion.

HASHI owns exactly one PCM document per Agent workspace: ``agent.md``.  This
module deliberately has no filename fallback policy.  Callers either provide
that canonical path or receive a typed validation failure.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final


PCM_FILENAME: Final = "agent.md"
PCM_BLOCKS: Final[tuple[str, ...]] = ("persona", "sys", "memory")
PCM_REQUIRED_BLOCKS: Final[frozenset[str]] = frozenset({"persona", "sys"})
LEGACY_DEFAULT_SYS: Final = (
    "Follow the configured Persona while obeying HASHI infrastructure policy, "
    "active /sys instructions, and the authoritative current user request."
)
LEGACY_DEFAULT_PERSONA: Final = "You are the configured HASHI agent."


class PCMValidationError(ValueError):
    """A fail-closed, machine-readable PCM validation failure."""

    def __init__(self, code: str, message: str, *, path: Path | None = None):
        self.code = str(code)
        self.path = Path(path) if path is not None else None
        prefix = f"{self.path}: " if self.path is not None else ""
        super().__init__(f"{prefix}{message}")


@dataclass(frozen=True)
class PCMDocument:
    path: Path | None
    persona: str
    system: str
    memory: str
    content_sha256: str

    def block(self, name: str) -> str:
        if name == "persona":
            return self.persona
        if name == "sys":
            return self.system
        if name == "memory":
            return self.memory
        raise KeyError(name)

    def audit_fields(self) -> dict[str, object]:
        return {
            "pcm_source_label": self.path.name if self.path is not None else None,
            "pcm_content_sha256": self.content_sha256,
            "pcm_persona_chars": len(self.persona),
            "pcm_system_chars": len(self.system),
            "pcm_memory_chars": len(self.memory),
        }


def canonical_agent_md(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / PCM_FILENAME


def _marker_kind(line: str) -> tuple[str, bool] | None:
    stripped = line.strip()
    for name in PCM_BLOCKS:
        if stripped == f"[{name}]":
            return name, True
        if stripped == f"[{name}_end]":
            return name, False
    return None


def parse_pcm_text(text: str, *, path: Path | None = None) -> PCMDocument:
    """Parse one strict PCM document without accepting unmarked prose."""

    if not isinstance(text, str):
        raise PCMValidationError("pcm_not_text", "PCM content must be text", path=path)

    blocks: dict[str, list[str]] = {}
    active: str | None = None
    active_lines: list[str] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = _marker_kind(line)
        if marker is not None:
            name, opening = marker
            if opening:
                if active is not None:
                    raise PCMValidationError(
                        "pcm_nested_block",
                        f"[{name}] starts inside [{active}] at line {line_number}",
                        path=path,
                    )
                if name in blocks:
                    raise PCMValidationError(
                        "pcm_duplicate_block",
                        f"[{name}] appears more than once",
                        path=path,
                    )
                active = name
                active_lines = []
                continue
            if active != name:
                raise PCMValidationError(
                    "pcm_mismatched_block",
                    f"[{name}_end] has no matching [{name}] at line {line_number}",
                    path=path,
                )
            blocks[name] = list(active_lines)
            active = None
            active_lines = []
            continue

        if active is None:
            if line.strip():
                raise PCMValidationError(
                    "pcm_unmarked_content",
                    f"substantive content outside a recognised block at line {line_number}",
                    path=path,
                )
            continue
        active_lines.append(line)

    if active is not None:
        raise PCMValidationError(
            "pcm_unclosed_block",
            f"[{active}] is not closed",
            path=path,
        )

    missing = sorted(PCM_REQUIRED_BLOCKS - set(blocks))
    if missing:
        raise PCMValidationError(
            "pcm_required_block_missing",
            "missing required block(s): " + ", ".join(missing),
            path=path,
        )

    normalized: dict[str, str] = {
        name: "\n".join(lines).strip() for name, lines in blocks.items()
    }
    for name, value in normalized.items():
        if not value:
            raise PCMValidationError(
                "pcm_empty_block",
                f"[{name}] must not be empty",
                path=path,
            )

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PCMDocument(
        path=path,
        persona=normalized["persona"],
        system=normalized["sys"],
        memory=normalized.get("memory", ""),
        content_sha256=digest,
    )


def load_pcm_document(
    path: str | Path,
    *,
    workspace_dir: str | Path | None = None,
) -> PCMDocument:
    """Load exact lower-case ``agent.md`` as strict UTF-8."""

    source = Path(path).expanduser()
    if source.name != PCM_FILENAME:
        raise PCMValidationError(
            "pcm_noncanonical_filename",
            f"PCM filename must be exactly {PCM_FILENAME!r}",
            path=source,
        )
    if workspace_dir is not None:
        workspace_root = Path(workspace_dir).expanduser().resolve(strict=False)
        try:
            same_parent = source.parent.resolve(strict=False) == workspace_root
        except OSError:
            same_parent = source.parent.absolute() == workspace_root.absolute()
        if not same_parent:
            raise PCMValidationError(
                "pcm_outside_workspace",
                "PCM must be the canonical agent.md in the Agent workspace",
                path=source,
            )
        if source.is_symlink():
            raise PCMValidationError(
                "pcm_symlink_forbidden",
                "canonical agent.md must be a regular workspace-owned file, not a symlink",
                path=source,
            )
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        raise PCMValidationError(
            "pcm_missing", "canonical agent.md does not exist", path=source
        ) from exc
    except OSError as exc:
        raise PCMValidationError(
            "pcm_unreadable", f"canonical agent.md cannot be read: {type(exc).__name__}", path=source
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PCMValidationError(
            "pcm_invalid_utf8", "canonical agent.md is not valid UTF-8", path=source
        ) from exc
    document = parse_pcm_text(text, path=source)
    return PCMDocument(
        path=source,
        persona=document.persona,
        system=document.system,
        memory=document.memory,
        content_sha256=hashlib.sha256(raw).hexdigest(),
    )


def render_pcm_document(*, persona: str, system: str, memory: str = "") -> str:
    values = {
        "persona": str(persona or "").strip(),
        "sys": str(system or "").strip(),
        "memory": str(memory or "").strip(),
    }
    if not values["persona"] or not values["sys"]:
        raise PCMValidationError(
            "pcm_required_block_empty",
            "persona and sys content are required",
        )
    parts = [
        f"[persona]\n{values['persona']}\n[persona_end]",
        f"[sys]\n{values['sys']}\n[sys_end]",
    ]
    if values["memory"]:
        parts.append(f"[memory]\n{values['memory']}\n[memory_end]")
    rendered = "\n\n".join(parts) + "\n"
    parse_pcm_text(rendered)
    return rendered


def _legacy_block_span(text: str, name: str) -> tuple[str, tuple[int, int] | None]:
    begin_matches = list(
        re.finditer(rf"(?m)^[ \t]*\[{re.escape(name)}\][ \t]*$", text)
    )
    end_matches = list(
        re.finditer(rf"(?m)^[ \t]*\[{re.escape(name)}_end\][ \t]*$", text)
    )
    if not begin_matches and not end_matches:
        return "", None
    if len(begin_matches) != 1 or len(end_matches) != 1:
        raise PCMValidationError(
            "pcm_legacy_block_ambiguous",
            f"legacy {name} block markers are missing or repeated",
        )
    begin_match = begin_matches[0]
    end_match = end_matches[0]
    begin = begin_match.start()
    content_begin = begin_match.end()
    end = end_match.start()
    if end < content_begin:
        raise PCMValidationError(
            "pcm_legacy_block_reversed",
            f"legacy {name} block markers are reversed",
        )
    return text[content_begin:end].strip(), (begin, end_match.end())


def convert_legacy_pcm_text(text: str) -> str:
    """Convert one legacy identity document without silently dropping prose."""

    try:
        parse_pcm_text(text)
        return text if text.endswith("\n") else text + "\n"
    except PCMValidationError:
        pass

    extracted: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for name in PCM_BLOCKS:
        value, span = _legacy_block_span(text, name)
        if span is not None:
            if not value:
                raise PCMValidationError(
                    "pcm_legacy_block_empty", f"legacy [{name}] block is empty"
                )
            extracted[name] = value
            spans.append(span)

    remainder_parts: list[str] = []
    cursor = 0
    for begin, end in sorted(spans):
        if begin < cursor:
            raise PCMValidationError(
                "pcm_legacy_block_overlap", "legacy PCM blocks overlap"
            )
        remainder_parts.append(text[cursor:begin])
        cursor = end
    remainder_parts.append(text[cursor:])
    remainder = "\n".join(part.strip() for part in remainder_parts if part.strip()).strip()

    if not extracted:
        persona = text.strip()
        system = LEGACY_DEFAULT_SYS
    else:
        persona = extracted.get("persona") or remainder or LEGACY_DEFAULT_PERSONA
        system = extracted.get("sys") or (
            remainder if extracted.get("persona") else LEGACY_DEFAULT_SYS
        ) or LEGACY_DEFAULT_SYS
        if remainder and extracted.get("persona") and extracted.get("sys"):
            system = system + "\n\nLegacy unmarked guidance preserved during migration:\n" + remainder
    memory = extracted.get("memory", "")
    if not persona:
        raise PCMValidationError(
            "pcm_legacy_empty", "legacy PCM source contains no usable content"
        )
    return render_pcm_document(persona=persona, system=system, memory=memory)


def atomic_write_pcm(path: str | Path, content: str) -> Path:
    """Validate, then atomically install an owner-readable PCM document."""

    target = Path(path)
    if target.name != PCM_FILENAME:
        raise PCMValidationError(
            "pcm_noncanonical_filename",
            f"PCM filename must be exactly {PCM_FILENAME!r}",
            path=target,
        )
    parse_pcm_text(content, path=target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.pcm-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return target
