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
    "background_maintenance": frozenset({"maintenance_prompt"}),
    "execution_request": frozenset(
        {
            "active_plan_section",
            "assignment_section",
            "goal",
            "schema",
            "sub_agent_results_section",
        }
    ),
    "finalisation_request": frozenset({"context", "goal", "schema"}),
    "immediate_response_request": frozenset({"goal"}),
    "stage_request": frozenset(
        {"context", "reviewer_rule", "schema", "sub_agent_rule"}
    ),
    "system_dream": frozenset(),
    "system_execution": frozenset(),
    "system_checkpoint": frozenset(),
    "system_finalisation": frozenset(
        {"persona_block_begin", "persona_block_end", "persona_guidance"}
    ),
    "system_immediate_response": frozenset(
        {"persona_block_begin", "persona_block_end", "persona_guidance"}
    ),
    "system_meditation": frozenset(),
    "system_persona_commentary": frozenset(
        {"persona_block_begin", "persona_block_end", "persona_guidance"}
    ),
    "system_persona_required_message": frozenset(
        {
            "kind_rule",
            "message_kind",
            "persona_block_begin",
            "persona_block_end",
            "persona_guidance",
        }
    ),
    "system_planning": frozenset(),
    "system_replanning": frozenset(),
    "system_review": frozenset(),
    "system_verification": frozenset(),
    "system_sub_agent": frozenset(),
    "system_triage": frozenset(),
    "triage_request": frozenset({"goal", "schema"}),
    "triage_retry": frozenset({"prompt", "retry_feedback"}),
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
