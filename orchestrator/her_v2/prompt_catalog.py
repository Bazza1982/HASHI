"""Load versioned HER v2 prompt templates from repository assets."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Mapping


class PromptAssetError(RuntimeError):
    """Raised when a required HER v2 prompt asset is missing or malformed."""


PROMPT_ASSET_ROOT = Path(__file__).with_name("prompt_assets")

# Keep the expected placeholders beside the loader so a typo in an edited
# prompt fails during import/hot reload instead of silently changing a model
# invocation.
PROMPT_ASSET_FIELDS: Mapping[str, frozenset[str]] = {
    "system_direct": frozenset(
        {
            "goal",
            "habit_catalogue",
            "direct_strategy_block",
            "persona_block_begin",
            "persona_block_end",
            "persona_guidance",
            "skills_catalogue",
            "tool_catalogue",
        }
    ),
    "system_dream": frozenset(),
    "system_dream_report": frozenset(),
    "system_execution": frozenset(
        {
            "active_plan",
            "delegated_execution",
            "goal",
            "persona_block_begin",
            "persona_block_end",
            "persona_guidance",
            "relevant_habits",
            "strategy_handoff",
            "tool_catalogue",
        }
    ),
    "system_finalisation": frozenset(
        {
            "completion_evidence",
            "draft_response",
            "goal",
            "persona_block_begin",
            "persona_block_end",
            "persona_guidance",
            "relevant_habits",
            "reviewer_findings",
        }
    ),
    "system_immediate_response": frozenset(
        {"goal", "persona_block_begin", "persona_block_end", "persona_guidance"}
    ),
    "system_json_repair": frozenset(),
    "system_meditation": frozenset(),
    "system_persona_commentary": frozenset(
        {"persona_block_begin", "persona_block_end", "persona_guidance"}
    ),
    "system_planning": frozenset(
        {
            "available_execution_tools",
            "available_sub_agent_profiles",
            "classification",
            "goal",
            "relevant_habits",
            "schema",
            "stage_tool_policy",
            "strategy_handoff",
        }
    ),
    "system_replanning": frozenset(
        {
            "active_plan",
            "available_execution_tools",
            "available_sub_agent_profiles",
            "classification",
            "goal",
            "plan_edit_history",
            "relevant_habits",
            "schema",
            "workflow_state_and_evidence",
        }
    ),
    "system_review": frozenset(
        {
            "available_review_tools",
            "draft_response",
            "execution_evidence",
            "goal",
            "review_context",
        }
    ),
    "system_sub_agent": frozenset(
        {
            "active_plan",
            "assignment",
            "real_goal",
            "relevant_habits",
            "schema",
            "sub_agent_results",
        }
    ),
    "system_triage": frozenset({"goal", "habit_catalogue", "schema_v2"}),
    "system_strategy": frozenset(
        {
            "execution_capabilities",
            "goal",
            "habit_catalogue",
            "request_resources",
            "schema_v3",
            "stage_tool_policy",
            "strategy_cards",
        }
    ),
}


def _template_fields(template: Template) -> frozenset[str]:
    fields: set[str] = set()
    for match in template.pattern.finditer(template.template):
        if match.group("invalid") is not None:
            raise PromptAssetError("prompt template contains an invalid '$' token")
        identifier = match.group("named") or match.group("braced")
        if identifier:
            fields.add(identifier)
    return frozenset(fields)


@lru_cache(maxsize=None)
def load_prompt_asset(name: str) -> str:
    """Return one validated prompt template without depending on the cwd."""

    asset_name = str(name or "").strip()
    expected_fields = PROMPT_ASSET_FIELDS.get(asset_name)
    if expected_fields is None:
        raise PromptAssetError(f"unknown HER v2 prompt asset: {asset_name!r}")
    path = PROMPT_ASSET_ROOT / f"{asset_name}.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptAssetError(
            f"cannot read required HER v2 prompt asset {asset_name!r}: {exc}"
        ) from exc
    if not text:
        raise PromptAssetError(f"HER v2 prompt asset {asset_name!r} is empty")
    template = Template(text)
    actual_fields = _template_fields(template)
    if actual_fields != expected_fields:
        raise PromptAssetError(
            f"HER v2 prompt asset {asset_name!r} placeholders are invalid: "
            f"expected {sorted(expected_fields)}, found {sorted(actual_fields)}"
        )
    return text


def render_prompt_asset(name: str, /, **values: object) -> str:
    """Render one prompt template with an exact, validated value set."""

    expected_fields = PROMPT_ASSET_FIELDS.get(name)
    if expected_fields is None:
        raise PromptAssetError(f"unknown HER v2 prompt asset: {name!r}")
    actual_fields = frozenset(values)
    if actual_fields != expected_fields:
        raise PromptAssetError(
            f"HER v2 prompt asset {name!r} values are invalid: "
            f"expected {sorted(expected_fields)}, found {sorted(actual_fields)}"
        )
    return Template(load_prompt_asset(name)).substitute(
        {key: str(value) for key, value in values.items()}
    )


def validate_prompt_assets() -> None:
    """Fail closed when any required external prompt is unavailable."""

    for name in PROMPT_ASSET_FIELDS:
        load_prompt_asset(name)


validate_prompt_assets()
