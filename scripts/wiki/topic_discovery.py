"""AI topic discovery support for the HASHI wiki curation pipeline."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .backend_client import BackendCallResult, call_lily_cli_backend
from .config import WikiConfig


@dataclass(frozen=True)
class DiscoveryMemory:
    consolidated_id: int
    current_topic_id: str
    confidence: float
    agent_id: str
    domain: str
    memory_type: str
    content: str
    source_ts: str


@dataclass(frozen=True)
class TopicCandidate:
    proposed_topic_id: str
    display: str
    description: str
    topic_type: str
    aliases: tuple[str, ...]
    evidence_ids: tuple[int, ...]
    source_terms: tuple[str, ...]
    recommended_action: str
    merge_target: str | None
    confidence: float
    quality_score: float | None
    uncertainty_score: float | None
    privacy_level: str
    curator_reason: str

    @property
    def candidate_id(self) -> str:
        return self.proposed_topic_id


@dataclass(frozen=True)
class TopicDiscoveryResult:
    candidates: list[TopicCandidate]
    backend: str
    model: str
    raw_chars: int


def fetch_discovery_memories(
    config: WikiConfig,
    *,
    limit: int = 100,
    full_library_scan: bool = False,
) -> list[DiscoveryMemory]:
    """Fetch recent memories that should be reviewed for topic novelty."""
    if not config.wiki_state_db.exists() or not config.consolidated_db.exists():
        return []
    con = sqlite3.connect(_readonly_uri(config.wiki_state_db), uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("ATTACH DATABASE ? AS mem", (_readonly_uri(config.consolidated_db),))
        where_clause = (
            "a.status = 'ok'"
            if full_library_scan
            else """
            a.status = 'ok'
              AND (
                    a.topic_id IN ('UNCATEGORIZED_REVIEW', 'NONE')
                    OR a.confidence < 0.75
                  )
            """
        )
        rows = con.execute(
            f"""
            SELECT
                a.consolidated_id,
                a.topic_id,
                a.confidence,
                c.agent_id,
                c.domain,
                c.memory_type,
                c.content,
                c.source_ts
            FROM classification_assignment AS a
            JOIN mem.consolidated AS c ON c.id = a.consolidated_id
            WHERE {where_clause}
            ORDER BY a.consolidated_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [
        DiscoveryMemory(
            consolidated_id=int(row["consolidated_id"]),
            current_topic_id=row["topic_id"],
            confidence=float(row["confidence"]),
            agent_id=row["agent_id"],
            domain=row["domain"],
            memory_type=row["memory_type"],
            content=_truncate_context(row["content"]),
            source_ts=row["source_ts"],
        )
        for row in rows
    ]


def discover_topic_candidates(
    memories: list[DiscoveryMemory],
    active_topics: dict[str, dict[str, str]],
    config: WikiConfig,
    *,
    mock: bool = False,
) -> TopicDiscoveryResult:
    if not memories:
        return TopicDiscoveryResult(candidates=[], backend="none", model="none", raw_chars=0)
    if mock:
        return _mock_discover(memories)
    prompt = build_topic_discovery_prompt(memories, active_topics)
    call = call_lily_cli_backend(prompt, config)
    return parse_topic_discovery_response(call, memories=memories)


def build_topic_discovery_prompt(
    memories: list[DiscoveryMemory],
    active_topics: dict[str, dict[str, str]],
) -> str:
    active_topics_payload = [
        {
            "topic_id": topic_id,
            "display": meta["display"],
            "description": meta["desc"],
            "topic_type": meta.get("topic_type", "concept"),
        }
        for topic_id, meta in sorted(active_topics.items())
    ]
    memory_payload = [
        {
            "id": memory.consolidated_id,
            "current_topic_id": memory.current_topic_id,
            "confidence": memory.confidence,
            "agent": memory.agent_id,
            "domain": memory.domain,
            "memory_type": memory.memory_type,
            "source_ts": memory.source_ts,
            "content": memory.content,
        }
        for memory in memories
    ]
    return f"""You are the AI curator for a multi-agent knowledge wiki.
Your job is not fixed-label classification. Your job is to identify durable knowledge topics that deserve first-class wiki pages, or to recommend merge/split/retire review for existing topics.

Active topics:
{json.dumps(active_topics_payload, ensure_ascii=False, indent=2)}

Candidate source memories:
{json.dumps(memory_payload, ensure_ascii=False, indent=2)}

Rules:
1. Use AI judgement. Do not promote a topic merely because a keyword repeats.
2. Prefer durable projects, systems, tools, games, workflows, research streams, or concepts that agents will need later.
3. Recommend merge when the candidate truly belongs inside an existing topic.
4. Recommend split when an existing topic is too broad.
5. Privacy-sensitive or relationship content must use privacy_level="private_blocked" and recommended_action="ignore".
6. Fixed thresholds are not the promotion logic. Provide a concrete curator_reason.
7. Return only valid JSON, no markdown.

Output schema:
[
  {{
    "proposed_topic_id": "Manchuria_Game",
    "display": "Manchuria Game",
    "description": "Durable scope description.",
    "topic_type": "game",
    "aliases": ["Manchuria"],
    "evidence_ids": [123],
    "source_terms": ["manchuria"],
    "recommended_action": "promote",
    "merge_target": null,
    "confidence": 0.91,
    "quality_score": 0.86,
    "uncertainty_score": 0.12,
    "privacy_level": "internal",
    "curator_reason": "Why this deserves action."
  }}
]
"""


def parse_topic_discovery_response(
    call: BackendCallResult,
    *,
    memories: list[DiscoveryMemory],
) -> TopicDiscoveryResult:
    evidence_ids = {memory.consolidated_id for memory in memories}
    payload = _extract_json_array(call.text)
    candidates: list[TopicCandidate] = []
    for item in payload:
        ids = tuple(int(value) for value in item.get("evidence_ids") or [])
        if not ids or any(value not in evidence_ids for value in ids):
            continue
        candidates.append(
            TopicCandidate(
                proposed_topic_id=str(item["proposed_topic_id"]),
                display=str(item["display"]),
                description=str(item["description"]),
                topic_type=str(item.get("topic_type") or "concept"),
                aliases=tuple(str(value) for value in item.get("aliases") or []),
                evidence_ids=ids,
                source_terms=tuple(str(value) for value in item.get("source_terms") or []),
                recommended_action=str(item["recommended_action"]),
                merge_target=item.get("merge_target"),
                confidence=float(item.get("confidence", 0.0)),
                quality_score=_optional_float(item.get("quality_score")),
                uncertainty_score=_optional_float(item.get("uncertainty_score")),
                privacy_level=str(item.get("privacy_level") or "internal"),
                curator_reason=str(item["curator_reason"]),
            )
        )
    return TopicDiscoveryResult(
        candidates=candidates,
        backend=call.backend,
        model=call.model,
        raw_chars=len(call.text or ""),
    )


def persist_topic_candidates(state, candidates: list[TopicCandidate]) -> None:
    for candidate in candidates:
        state.upsert_topic_candidate(
            candidate_id=candidate.candidate_id,
            proposed_topic_id=candidate.proposed_topic_id,
            display=candidate.display,
            description=candidate.description,
            topic_type=candidate.topic_type,
            evidence_ids=list(candidate.evidence_ids),
            curator_reason=candidate.curator_reason,
            recommended_action=candidate.recommended_action,
            confidence=candidate.confidence,
            aliases=list(candidate.aliases),
            source_terms=list(candidate.source_terms),
            merge_target=candidate.merge_target,
            quality_score=candidate.quality_score,
            uncertainty_score=candidate.uncertainty_score,
            privacy_level=candidate.privacy_level,
        )


def build_topic_candidates_page(candidates: list[TopicCandidate]) -> str:
    lines = [
        "---",
        'index_id: "TOPIC_CANDIDATES"',
        'title: "Topic Candidates"',
        "status: dry-run",
        f"candidate_count: {len(candidates)}",
        "---",
        "",
        "# Topic Candidates",
        "",
        "<!-- WIKI-GENERATED: dry-run topic governance queue. -->",
        "",
    ]
    if not candidates:
        lines.extend(["No pending candidates.", ""])
        return "\n".join(lines).rstrip() + "\n"
    for candidate in sorted(candidates, key=lambda item: item.proposed_topic_id):
        lines.extend(
            [
                f"## {candidate.display}",
                "",
                f"- Proposed ID: `{candidate.proposed_topic_id}`",
                f"- Type: `{candidate.topic_type}`",
                f"- Action: `{candidate.recommended_action}`",
                f"- Confidence: `{candidate.confidence:.2f}`",
                f"- Quality: `{candidate.quality_score:.2f}`" if candidate.quality_score is not None else "- Quality: `unknown`",
                f"- Uncertainty: `{candidate.uncertainty_score:.2f}`" if candidate.uncertainty_score is not None else "- Uncertainty: `unknown`",
                f"- Privacy: `{candidate.privacy_level}`",
                f"- Evidence IDs: `{', '.join(str(value) for value in candidate.evidence_ids)}`",
                "",
                candidate.curator_reason,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _mock_discover(memories: list[DiscoveryMemory]) -> TopicDiscoveryResult:
    candidates: list[TopicCandidate] = []
    buckets = {
        "Manchuria_Game": ("Manchuria Game", "game", ("manchuria", "ai mud", "奉天城")),
        "KASUMI_Software": ("KASUMI Software", "tool", ("kasumi",)),
        "WORDO": ("WORDO", "tool", ("wordo",)),
    }
    for topic_id, (display, topic_type, terms) in buckets.items():
        matched = [
            memory
            for memory in memories
            if any(term in memory.content.lower() for term in terms)
        ]
        if not matched:
            continue
        candidates.append(
            TopicCandidate(
                proposed_topic_id=topic_id,
                display=display,
                description=f"{display} durable knowledge topic discovered from wiki memories.",
                topic_type=topic_type,
                aliases=(display,),
                evidence_ids=tuple(memory.consolidated_id for memory in matched[:10]),
                source_terms=terms,
                recommended_action="promote",
                merge_target=None,
                confidence=0.9,
                quality_score=0.85,
                uncertainty_score=0.1,
                privacy_level="internal",
                curator_reason=f"Mock curator found durable references for {display}.",
            )
        )
    return TopicDiscoveryResult(candidates=candidates, backend="mock", model="mock", raw_chars=0)


def _extract_json_array(text: str):
    stripped = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", stripped, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", stripped):
        try:
            payload, _ = decoder.raw_decode(stripped[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            return payload
    raise ValueError("Topic discovery output did not contain a JSON candidate array")


def _optional_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _readonly_uri(path) -> str:
    return f"file:{path}?mode=ro"


def _truncate_context(content: str, limit: int = 1200) -> str:
    compact = " ".join((content or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
