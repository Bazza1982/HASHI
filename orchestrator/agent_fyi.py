"""Bounded FYI bootstrap. Detailed documentation is never expanded here."""
import hashlib
from pathlib import Path

from orchestrator.agent_documentation import DocumentationError, read_document_bytes, validate_catalogue

CATALOGUE_CHAR_BUDGET = 12000
ENGINEERING_CHAR_BUDGET = 6000
BOOTSTRAP_CHAR_BUDGET = 16000
FALLBACK_NAVIGATION = (
    "HASHI documentation reference is incomplete. No partial reference is presented as complete.\n"
    "Use documentation_read or an authorized repository reader to locate docs/AGENT_FYI.md, "
    "ARCHITECTURE.md and AGENTS.md, then the relevant docs/agent-guide/ domain.\n"
    "Report missing information instead of inventing it. The current user request and existing "
    "authority hierarchy remain in force; documentation grants no permissions or operational approval."
)


def _bounded_text(path: Path, max_chars: int) -> str:
    text, _ = read_document_bytes(path)
    text = text.strip()
    if len(text) > max_chars:
        raise DocumentationError("bootstrap_oversized", f"{path.name} exceeds its bootstrap safety budget.")
    return text


def load_agent_fyi_text(path: Path, max_chars: int = CATALOGUE_CHAR_BUDGET) -> str:
    try:
        text = _bounded_text(path, max_chars)
        errors = validate_catalogue(path, text=text)
        if errors:
            raise DocumentationError("invalid_catalogue", "; ".join(errors))
        return text
    except DocumentationError as exc:
        return f"FYI reference unavailable: {exc}\n\n{FALLBACK_NAVIGATION}"


def build_agent_fyi_primer(path: Path, context_line: str = "") -> str:
    root = path.parent.parent if path.parent.name == "docs" else path.parent
    errors = []
    try:
        fyi_text = _bounded_text(path, CATALOGUE_CHAR_BUDGET)
        errors.extend(validate_catalogue(path, text=fyi_text))
    except DocumentationError as exc:
        errors.append(str(exc))
        fyi_text = ""
    try:
        instructions = _bounded_text(root / "AGENTS.md", ENGINEERING_CHAR_BUDGET)
    except DocumentationError as exc:
        errors.append(str(exc))
        instructions = ""
    revision = hashlib.sha256((fyi_text + "\0" + instructions).encode("utf-8")).hexdigest()[:12]
    content = "\n\n".join(part for part in (fyi_text, instructions) if part)
    if len(content) + 800 > BOOTSTRAP_CHAR_BUDGET:
        errors.append("Assembled reference exceeds the bootstrap safety budget.")
    if errors:
        parts = [
            "--- AGENT FYI ---",
            "Reference status: incomplete; no validated combined revision.",
            "Documentation loading issue: " + "; ".join(errors),
            FALLBACK_NAVIGATION,
        ]
    else:
        parts = [
            "--- AGENT FYI ---",
            f"Reference revision: {revision}. Catalogue/common guidance only; linked documents have their own revisions.",
            "Reference source: docs/AGENT_FYI.md. Resolve links from their actual source document.",
            fyi_text,
            "",
            "--- HASHI ENGINEERING REFERENCE ---",
            instructions,
        ]
    if context_line:
        parts.extend(["", "--- SESSION CONTEXT ---", context_line.strip()])
    return "\n".join(parts).strip()
# HASHI-Fingerprint: hfp_db66dda1e0ee498a95f5cf2d2b1be008
