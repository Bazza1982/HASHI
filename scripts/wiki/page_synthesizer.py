"""Claim-backed wiki page synthesis helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicClaim:
    claim: str
    section: str
    evidence_ids: tuple[int, ...]
    confidence: float
    claim_type: str


def synthesize_claims_from_memories(topic_id: str, memories) -> list[TopicClaim]:
    """Create structured, evidence-backed claims from topic evidence.

    This is the local deterministic synthesis path. A later AI synthesizer can
    replace claim text generation while preserving this claim contract.
    """
    claims: list[TopicClaim] = []
    for memory in memories:
        text = _compact(memory.content)
        if not text:
            continue
        section = "Stable Facts" if memory.confidence >= 0.75 else "Open Questions / Risks"
        claims.append(
            TopicClaim(
                claim=text,
                section=section,
                evidence_ids=(memory.consolidated_id,),
                confidence=memory.confidence,
                claim_type="current_state" if section == "Stable Facts" else "open_question",
            )
        )
    validate_claims(claims, evidence_ids={memory.consolidated_id for memory in memories})
    return claims


def validate_claims(claims: list[TopicClaim], *, evidence_ids: set[int]) -> None:
    for claim in claims:
        if not claim.claim.strip():
            raise ValueError("Topic claim is empty")
        if not claim.evidence_ids:
            raise ValueError(f"Topic claim has no evidence: {claim.claim}")
        missing = [evidence_id for evidence_id in claim.evidence_ids if evidence_id not in evidence_ids]
        if missing:
            raise ValueError(f"Topic claim references missing evidence ids: {missing}")
        if claim.section == "Stable Facts" and claim.confidence < 0.75:
            raise ValueError("Stable Facts claim has insufficient confidence")


def render_claim_sections(claims: list[TopicClaim]) -> list[str]:
    sections: list[str] = []
    for section in ("Current State", "Stable Facts", "Key Decisions", "Recent Changes", "Open Questions / Risks"):
        section_claims = [claim for claim in claims if claim.section == section]
        if not section_claims and section != "Current State":
            continue
        sections.extend([f"## {section}", ""])
        if not section_claims:
            sections.extend(["No validated claims yet.", ""])
            continue
        for claim in section_claims:
            evidence = ", ".join(str(value) for value in claim.evidence_ids)
            sections.append(
                f"- {claim.claim} "
                f"<!-- claim_type={claim.claim_type}; evidence_ids={evidence}; confidence={claim.confidence:.2f} -->"
            )
        sections.append("")
    return sections


def _compact(content: str, limit: int = 420) -> str:
    text = " ".join((content or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."
