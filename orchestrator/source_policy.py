from __future__ import annotations


HUMAN_HCHAT_SOURCES = frozenset({"bridge:hchat", "bridge:hchat-draft"})

REMOTE_API_AUTOMATED_PREFIXES = (
    "scheduler",
    "bridge:",
    "bridge-transfer:",
    "hchat-reply:",
    "cos-query:",
    "ticket:",
    "loop_skill",
    "startup",
)


def normalize_source(source: str | None) -> str:
    return (source or "").strip().lower()


def is_human_hchat_source(source: str | None) -> bool:
    normalized = normalize_source(source)
    return normalized in HUMAN_HCHAT_SOURCES or normalized.startswith("hchat-reply:")


def source_requires_manual_remote_api_permission(source: str | None) -> bool:
    normalized = normalize_source(source)
    if not normalized:
        return True
    if is_human_hchat_source(normalized):
        return False
    return normalized.startswith(REMOTE_API_AUTOMATED_PREFIXES)
