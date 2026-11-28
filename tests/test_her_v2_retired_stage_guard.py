"""Retired multi-stage Harness surface must stay gone.

HERV3 runs one direct loop; Triage/Planning/Replanning/Review/Finalisation were
retired.  These guards fail loudly if a later change silently re-wires one of
the retired stages into the invocation path, or restores a retired prompt
contract instead of a real caller.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.her_v2 import commentary, structured
from orchestrator.her_v2.models import Stage

import adapters.her_v2_provider as provider_module

_RETIRED = ("TRIAGE", "PLANNING", "REPLANNING", "REVIEW", "FINALISATION")


def _stage_references(source: str) -> list[str]:
    return [name for name in _RETIRED if f"Stage.{name}" in source]


def test_provider_adapter_has_no_retired_stage_branches() -> None:
    source = Path(provider_module.__file__).read_text(encoding="utf-8")

    assert _stage_references(source) == []


def test_runtime_invocation_has_no_retired_stage_branches() -> None:
    source = (
        Path(commentary.__file__).with_name("runtime_invocation.py").read_text(
            encoding="utf-8"
        )
    )

    assert _stage_references(source) == []


def test_commentary_lane_is_direct_and_execution_only() -> None:
    assert commentary.COMMENTARY_STAGES == frozenset({Stage.DIRECT, Stage.EXECUTION})


def test_retired_stage_parsers_are_not_restored() -> None:
    for name in (
        "parse_triage",
        "parse_strategy",
        "parse_plan",
        "parse_replanning",
        "parse_review",
        "validate_review_response",
        "parse_finalisation",
        "parse_immediate",
        "parse_execution_message",
    ):
        assert not hasattr(structured, name), name
# HASHI-Fingerprint: hfp_ff5fb84695e041d0bcda82eb0b7c2c7b
