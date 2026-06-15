from __future__ import annotations

from orchestrator.enterprise.policy import PolicyEvaluator, PolicyRule


def install_default_connector_policy(evaluator: PolicyEvaluator) -> list[PolicyRule]:
    """Install conservative connector defaults.

    Read-only GitHub metadata actions are explicitly allowed. GitHub write
    actions require approval by default so adding connector support does not
    silently expand enterprise write capability.
    """

    rules = [
        evaluator.add_rule(
            rule_id="tpl-connector-github-repo-read-allow",
            action="connector.execute",
            resource="connector:github:repo.read",
            effect="allow",
            priority=90,
        ),
        evaluator.add_rule(
            rule_id="tpl-connector-github-repo-get-allow",
            action="connector.execute",
            resource="connector:github:repo.get",
            effect="allow",
            priority=90,
        ),
    ]
    for action in ("issue.create", "pr.create", "pr.merge"):
        rules.append(
            evaluator.add_rule(
                rule_id=f"tpl-connector-github-{action.replace('.', '-')}-approval",
                action="connector.execute",
                resource=f"connector:github:{action}",
                effect="approval_required",
                priority=100,
            )
        )
    return rules
