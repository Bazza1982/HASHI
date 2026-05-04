"""LLM classifier for assigning consolidated memories to wiki topics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .backend_client import BackendCallResult, call_lily_cli_backend
from .config import TOPICS, WikiConfig
from .fetcher import MemoryRecord


@dataclass(frozen=True)
class ClassificationAssignment:
    consolidated_id: int
    topics: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class ClassificationDryRunResult:
    assignments: list[ClassificationAssignment]
    backend: str
    model: str
    raw_chars: int


def classify_memories_dry_run(
    memories: list[MemoryRecord],
    config: WikiConfig,
    *,
    mock: bool = False,
) -> ClassificationDryRunResult:
    if not memories:
        return ClassificationDryRunResult(assignments=[], backend="none", model="none", raw_chars=0)
    if mock:
        return _mock_classify(memories)
    prompt = build_classification_prompt(memories)
    call = call_lily_cli_backend(prompt, config)
    return parse_classification_response(call, memories)


def build_classification_prompt(memories: list[MemoryRecord]) -> str:
    topics_text = "\n".join(
        f"- {topic_id}: {meta['display']} — {meta['desc']}"
        for topic_id, meta in TOPICS.items()
    )
    payload = [
        {
            "id": record.id,
            "agent": record.agent_id,
            "domain": record.domain,
            "memory_type": record.memory_type,
            "content": record.content,
        }
        for record in memories
    ]
    return f"""You are a knowledge classifier for a multi-agent AI system wiki.
Assign each memory snippet to the correct topic(s) from the HASHI wiki taxonomy.

Topics:
{topics_text}

Rules:
1. Output only valid topic IDs from the list above.
2. Multi-topic only when content substantially addresses both topics.
3. "hashi" in content does NOT automatically mean HASHI_Architecture.
4. Low confidence (< 0.7) should use UNCATEGORIZED_REVIEW rather than forcing a topic.
5. Return only valid JSON, no markdown, no commentary.

Output format:
[
  {{"id": <consolidated_id>, "topics": ["TOPIC_ID"], "confidence": 0.95}}
]

Classify these memories:
{json.dumps(payload, ensure_ascii=False)}
"""


def parse_classification_response(
    call: BackendCallResult,
    memories: list[MemoryRecord],
) -> ClassificationDryRunResult:
    valid_ids = {record.id for record in memories}
    payload = _extract_json_array(call.text, valid_ids=valid_ids)
    valid_topics = set(TOPICS)
    assignments: list[ClassificationAssignment] = []
    for item in payload:
        consolidated_id = int(item["id"])
        if consolidated_id not in valid_ids:
            raise ValueError(f"Classifier returned unknown memory id: {consolidated_id}")
        topics = tuple(str(topic) for topic in item.get("topics") or [])
        if not topics:
            raise ValueError(f"Classifier returned no topics for memory id: {consolidated_id}")
        invalid = [topic for topic in topics if topic not in valid_topics]
        if invalid:
            raise ValueError(f"Classifier returned invalid topics for memory id {consolidated_id}: {invalid}")
        confidence = float(item.get("confidence", 0.0))
        assignments.append(
            ClassificationAssignment(
                consolidated_id=consolidated_id,
                topics=topics,
                confidence=confidence,
            )
        )
    return ClassificationDryRunResult(
        assignments=assignments,
        backend=call.backend,
        model=call.model,
        raw_chars=len(call.text or ""),
    )


def _extract_json_array(text: str, *, valid_ids: set[int] | None = None):
    stripped = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", stripped, re.S)
    if fenced:
        payload = _decode_first_assignment_array(fenced.group(1), valid_ids=valid_ids)
        if payload is not None:
            return payload
    payload = _decode_first_assignment_array(stripped, valid_ids=valid_ids)
    if payload is None:
        raise ValueError("Classifier output did not contain a JSON assignment array")
    return payload


def _decode_first_assignment_array(text: str, *, valid_ids: set[int] | None = None):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text or ""):
        try:
            payload, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if _looks_like_assignment_array(payload, valid_ids=valid_ids):
            return payload
    return None


def _looks_like_assignment_array(payload, *, valid_ids: set[int] | None = None) -> bool:
    if not isinstance(payload, list):
        return False
    if not all(isinstance(item, dict) and "id" in item for item in payload):
        return False
    if valid_ids is None:
        return True
    returned_ids = {int(item["id"]) for item in payload}
    return bool(returned_ids & valid_ids)


def _mock_classify(memories: list[MemoryRecord]) -> ClassificationDryRunResult:
    assignments = []
    for record in memories:
        lower = record.content.lower()
        if (
            "security" in lower
            or "permission" in lower
            or "firewall" in lower
            or "port" in lower
            or "chmod" in lower
            or "pip-audit" in lower
            or "vulnerability" in lower
            or "operational risk" in lower
        ):
            topics = ("HASHI_Ops_Security",)
        elif "memory" in lower or "consolidat" in lower or "embedding" in lower:
            topics = ("AI_Memory_Systems",)
        elif "wiki" in lower or "obsidian" in lower:
            topics = ("Obsidian_Wiki",)
        elif "nagare" in lower or "workflow" in lower:
            topics = ("Nagare_Workflow",)
        elif "carbon" in lower or "emission" in lower:
            topics = ("Carbon_Accounting",)
        elif "hashi" in lower or "scheduler" in lower:
            topics = ("HASHI_Architecture",)
        else:
            topics = ("UNCATEGORIZED_REVIEW",)
        assignments.append(
            ClassificationAssignment(
                consolidated_id=record.id,
                topics=topics,
                confidence=0.8,
            )
        )
    return ClassificationDryRunResult(
        assignments=assignments,
        backend="mock",
        model="mock",
        raw_chars=0,
    )
