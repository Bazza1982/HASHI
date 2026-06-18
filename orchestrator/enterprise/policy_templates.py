from __future__ import annotations

from orchestrator.enterprise.policy import PolicyEvaluator, PolicyRule


def install_default_connector_policy(evaluator: PolicyEvaluator) -> list[PolicyRule]:
    """Install conservative connector defaults.

    Read-only GitHub metadata actions are explicitly allowed. GitHub write
    actions and outbound chat webhook messages require approval by default so
    adding connector support does not silently expand enterprise write or data
    egress capability.
    """

    existing_ids = {rule.id for rule in evaluator.list_rules()}
    rules = []
    specs = [
        {
            "rule_id": "tpl-connector-github-repo-read-allow",
            "action": "connector.execute",
            "resource": "connector:github:repo.read",
            "effect": "allow",
            "priority": 90,
        },
        {
            "rule_id": "tpl-connector-github-repo-get-allow",
            "action": "connector.execute",
            "resource": "connector:github:repo.get",
            "effect": "allow",
            "priority": 90,
        },
    ]
    for action in ("issue.create", "pr.create", "pr.merge"):
        specs.append(
            {
                "rule_id": f"tpl-connector-github-{action.replace('.', '-')}-approval",
                "action": "connector.execute",
                "resource": f"connector:github:{action}",
                "effect": "approval_required",
                "priority": 100,
            }
        )
    specs.append(
        {
            "rule_id": "tpl-connector-slack-message-send-approval",
            "action": "connector.execute",
            "resource": "connector:slack:message.send",
            "effect": "approval_required",
            "priority": 100,
        }
    )
    specs.append(
        {
            "rule_id": "tpl-connector-google-chat-message-send-approval",
            "action": "connector.execute",
            "resource": "connector:google_chat:message.send",
            "effect": "approval_required",
            "priority": 100,
        }
    )
    specs.append(
        {
            "rule_id": "tpl-connector-teams-message-send-approval",
            "action": "connector.execute",
            "resource": "connector:teams:message.send",
            "effect": "approval_required",
            "priority": 100,
        }
    )
    for spec in specs:
        if spec["rule_id"] in existing_ids:
            rules.append(evaluator.get_rule(spec["rule_id"]))
        else:
            rules.append(evaluator.add_rule(**spec))
    return rules
