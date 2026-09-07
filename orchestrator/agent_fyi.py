import hashlib
from pathlib import Path


def load_agent_fyi_text(path: Path, max_chars: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 32].rstrip() + "\n\n[fyi trimmed]"


def build_agent_fyi_primer(path: Path, context_line: str = "") -> str:
    fyi_text = load_agent_fyi_text(path)
    if not fyi_text:
        return ""
    instructions = load_agent_fyi_text(path.parent.parent / "AGENTS.md", max_chars=4000)
    revision = hashlib.sha256((fyi_text + "\0" + instructions).encode("utf-8")).hexdigest()[:12]
    parts = [
        "--- AGENT FYI ---",
        f"Reference revision: {revision}. Documentation is not proof of live adoption.",
        fyi_text,
    ]
    if instructions:
        parts.extend(["", "--- HASHI ENGINEERING REFERENCE ---", instructions])
    if context_line:
        parts.extend(["", "--- SESSION CONTEXT ---", context_line.strip()])
    return "\n".join(parts).strip()
