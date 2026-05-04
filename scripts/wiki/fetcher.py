"""Stage 0 fetch and privacy filtering for the wiki pipeline."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import WikiConfig
from .state import WikiState


SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"[A-Za-z0-9_\-]{20,}:[A-Za-z0-9_\-]{30,}",
        r"sk-[A-Za-z0-9]{32,}",
        r"eyJ[A-Za-z0-9+/]{40,}",
        r"(?i)(password|passwd|secret|api.?key)\s*[=:]\s*\S+",
    )
)

PRIVATE_CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"爱的拥抱",
        r"深情的吻",
        r"亲一下",
        r"好想你",
        r"想念你",
        r"喜欢你",
        r"我爱你",
        r"(?i)\bintimate\b",
        r"(?i)\bromantic\b",
        r"(?i)\bpersonal life\b",
        r"(?i)\brelationship\b",
    )
)


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    instance: str
    agent_id: str
    domain: str
    memory_type: str
    content: str
    source_ts: str
    ts_source: str
    status: str = "classifiable"
    reason: str = ""


@dataclass(frozen=True)
class FetchResult:
    classifiable: list[MemoryRecord]
    skipped: list[MemoryRecord]
    redacted: list[MemoryRecord]
    max_seen_id: int
    source_ids: tuple[int, ...] = ()

    @property
    def total_seen(self) -> int:
        return len(self.classifiable) + len(self.skipped) + len(self.redacted)


def truncate_content(content: str, limit: int) -> str:
    text = " ".join((content or "").split())
    return text[:limit]


def has_sensitive_content(content: str) -> bool:
    return any(pattern.search(content or "") for pattern in SENSITIVE_PATTERNS)


def has_private_content(content: str) -> bool:
    return any(pattern.search(content or "") for pattern in PRIVATE_CONTENT_PATTERNS)


def filter_record(record: MemoryRecord, config: WikiConfig) -> MemoryRecord:
    if record.agent_id == "temp":
        return _replace_status(record, "skipped", "temp_agent")
    if record.domain in config.private_domains:
        return _replace_status(record, "skipped", f"private_domain:{record.domain}")
    if record.memory_type == "private":
        return _replace_status(record, "skipped", "private_memory_type")
    if len((record.content or "").strip()) < config.min_content_chars:
        return _replace_status(record, "skipped", "too_short")
    if has_private_content(record.content):
        return _replace_status(record, "skipped", "private_content_pattern")
    if has_sensitive_content(record.content):
        return MemoryRecord(
            id=record.id,
            instance=record.instance,
            agent_id=record.agent_id,
            domain=record.domain,
            memory_type=record.memory_type,
            content="[REDACTED: sensitive content detected]",
            source_ts=record.source_ts,
            ts_source=record.ts_source,
            status="redacted",
            reason="sensitive_pattern",
        )
    return MemoryRecord(
        id=record.id,
        instance=record.instance,
        agent_id=record.agent_id,
        domain=record.domain,
        memory_type=record.memory_type,
        content=truncate_content(record.content, config.classify_chars),
        source_ts=record.source_ts,
        ts_source=record.ts_source,
    )


def fetch_new_memories(
    config: WikiConfig,
    state: WikiState,
    *,
    limit: int | None = None,
) -> FetchResult:
    last_id = state.get_last_classified_id()
    query = """
        SELECT id, instance, agent_id, domain, memory_type, content, source_ts, ts_source
        FROM consolidated
        WHERE id > ?
        ORDER BY id ASC
    """
    params: list[object] = [last_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    con = _connect_readonly(config.consolidated_db)
    try:
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()
    classifiable: list[MemoryRecord] = []
    skipped: list[MemoryRecord] = []
    redacted: list[MemoryRecord] = []
    max_seen_id = last_id

    for row in rows:
        raw = MemoryRecord(
            id=int(row[0]),
            instance=row[1],
            agent_id=row[2],
            domain=row[3],
            memory_type=row[4],
            content=row[5] or "",
            source_ts=row[6],
            ts_source=row[7],
        )
        max_seen_id = max(max_seen_id, raw.id)
        filtered = filter_record(raw, config)
        if filtered.status == "classifiable":
            classifiable.append(filtered)
        elif filtered.status == "redacted":
            redacted.append(filtered)
        else:
            skipped.append(filtered)

    return FetchResult(
        classifiable=classifiable,
        skipped=skipped,
        redacted=redacted,
        max_seen_id=max_seen_id,
        source_ids=tuple(int(row[0]) for row in rows),
    )


def _replace_status(record: MemoryRecord, status: str, reason: str) -> MemoryRecord:
    return MemoryRecord(
        id=record.id,
        instance=record.instance,
        agent_id=record.agent_id,
        domain=record.domain,
        memory_type=record.memory_type,
        content=record.content,
        source_ts=record.source_ts,
        ts_source=record.ts_source,
        status=status,
        reason=reason,
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return con
