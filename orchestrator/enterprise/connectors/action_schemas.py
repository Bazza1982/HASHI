from __future__ import annotations

from copy import deepcopy
from typing import Any


_WEBHOOK_MESSAGE_PARAMETERS = (
    {
        "name": "text",
        "type": "string",
        "required": True,
        "description": "Outbound message text.",
    },
)

_ACTION_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "connector_type": "github",
        "action": "repo.read",
        "description": "Fetch repository metadata.",
        "resource": {"format": "repo:owner/name", "required": False},
        "dry_run_supported": True,
        "parameters": [
            {"name": "owner", "type": "string", "required": False, "description": "Repository owner."},
            {"name": "repo", "type": "string", "required": False, "description": "Repository name."},
        ],
    },
    {
        "connector_type": "github",
        "action": "repo.get",
        "description": "Fetch repository metadata.",
        "resource": {"format": "repo:owner/name", "required": False},
        "dry_run_supported": True,
        "parameters": [
            {"name": "owner", "type": "string", "required": False, "description": "Repository owner."},
            {"name": "repo", "type": "string", "required": False, "description": "Repository name."},
        ],
    },
    {
        "connector_type": "github",
        "action": "issue.create",
        "description": "Create a GitHub issue.",
        "resource": {"format": "repo:owner/name", "required": False},
        "dry_run_supported": True,
        "parameters": [
            {"name": "owner", "type": "string", "required": False, "description": "Repository owner."},
            {"name": "repo", "type": "string", "required": False, "description": "Repository name."},
            {"name": "title", "type": "string", "required": True, "description": "Issue title."},
            {"name": "body", "type": "string", "required": False, "description": "Issue body."},
            {"name": "labels", "type": "array", "required": False, "description": "Issue labels."},
        ],
    },
    {
        "connector_type": "github",
        "action": "pr.create",
        "description": "Create a GitHub pull request.",
        "resource": {"format": "repo:owner/name", "required": False},
        "dry_run_supported": True,
        "parameters": [
            {"name": "owner", "type": "string", "required": False, "description": "Repository owner."},
            {"name": "repo", "type": "string", "required": False, "description": "Repository name."},
            {"name": "title", "type": "string", "required": True, "description": "Pull request title."},
            {"name": "head", "type": "string", "required": True, "description": "Head branch."},
            {"name": "base", "type": "string", "required": True, "description": "Base branch."},
            {"name": "body", "type": "string", "required": False, "description": "Pull request body."},
            {"name": "draft", "type": "boolean", "required": False, "description": "Create as draft."},
        ],
    },
    {
        "connector_type": "github",
        "action": "pr.merge",
        "description": "Merge a GitHub pull request.",
        "resource": {"format": "repo:owner/name", "required": False},
        "dry_run_supported": True,
        "parameters": [
            {"name": "owner", "type": "string", "required": False, "description": "Repository owner."},
            {"name": "repo", "type": "string", "required": False, "description": "Repository name."},
            {"name": "pull_number", "type": "integer", "required": True, "description": "Pull request number."},
            {
                "name": "merge_method",
                "type": "string",
                "required": False,
                "enum": ["merge", "squash", "rebase"],
                "default": "merge",
                "description": "GitHub merge strategy.",
            },
            {"name": "commit_title", "type": "string", "required": False, "description": "Merge commit title."},
            {"name": "commit_message", "type": "string", "required": False, "description": "Merge commit message."},
            {"name": "sha", "type": "string", "required": False, "description": "Expected head SHA."},
        ],
    },
    {
        "connector_type": "slack",
        "action": "message.send",
        "description": "Send a Slack incoming-webhook message.",
        "resource": {"format": "*", "required": False},
        "dry_run_supported": True,
        "parameters": [
            *_WEBHOOK_MESSAGE_PARAMETERS,
            {"name": "blocks", "type": "array", "required": False, "description": "Slack Block Kit blocks."},
        ],
    },
    {
        "connector_type": "google_chat",
        "action": "message.send",
        "description": "Send a Google Chat incoming-webhook message.",
        "resource": {"format": "*", "required": False},
        "dry_run_supported": True,
        "parameters": [
            *_WEBHOOK_MESSAGE_PARAMETERS,
            {"name": "cards", "type": "array", "required": False, "description": "Google Chat cards."},
        ],
    },
    {
        "connector_type": "teams",
        "action": "message.send",
        "description": "Send a Teams incoming-webhook message.",
        "resource": {"format": "*", "required": False},
        "dry_run_supported": True,
        "parameters": [
            *_WEBHOOK_MESSAGE_PARAMETERS,
            {"name": "title", "type": "string", "required": False, "description": "Message title."},
            {"name": "sections", "type": "array", "required": False, "description": "Teams message sections."},
        ],
    },
    {
        "connector_type": "feishu",
        "action": "message.send",
        "description": "Send a Feishu/Lark incoming-webhook text message.",
        "resource": {"format": "*", "required": False},
        "dry_run_supported": True,
        "parameters": list(_WEBHOOK_MESSAGE_PARAMETERS),
    },
)


def connector_action_schemas() -> list[dict[str, Any]]:
    return deepcopy(list(_ACTION_SCHEMAS))


def connector_action_schema(connector_type: str, action: str) -> dict[str, Any] | None:
    connector_type = str(connector_type or "").strip().lower()
    action = str(action or "").strip().lower()
    for schema in _ACTION_SCHEMAS:
        if schema["connector_type"] == connector_type and schema["action"] == action:
            return deepcopy(schema)
    return None


def validate_connector_action_parameters(connector_type: str, action: str, parameters: dict[str, Any]) -> str | None:
    schema = connector_action_schema(connector_type, action)
    if schema is None:
        return None
    parameters = parameters if isinstance(parameters, dict) else {}
    for parameter in schema.get("parameters", []):
        name = str(parameter.get("name") or "").strip()
        value = parameters.get(name)
        value_missing = name not in parameters or _schema_value_missing(value, str(parameter.get("type") or ""))
        if parameter.get("required") and value_missing:
            return f"{schema['connector_type']}.{schema['action']} requires parameter {name}"
        if value_missing:
            continue
        parameter_type = str(parameter.get("type") or "").strip()
        if not _schema_value_matches_type(value, parameter_type):
            return f"{name} must be {parameter_type}"
        enum_values = parameter.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            return f"{name} must be one of {', '.join(str(item) for item in enum_values)}"
    return None


def _schema_value_missing(value: Any, parameter_type: str) -> bool:
    if value is None:
        return True
    if parameter_type == "string":
        return str(value).strip() == ""
    if parameter_type == "array":
        return not isinstance(value, list) or not value
    return False


def _schema_value_matches_type(value: Any, parameter_type: str) -> bool:
    if parameter_type == "string":
        return isinstance(value, str)
    if parameter_type == "array":
        return isinstance(value, list)
    if parameter_type == "boolean":
        return isinstance(value, bool)
    if parameter_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return True
