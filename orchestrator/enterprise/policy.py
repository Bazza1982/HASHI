from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"

    @classmethod
    def from_value(cls, value: str) -> "PolicyDecision":
        normalized = (value or "").strip().lower()
        for item in cls:
            if item.value == normalized:
                return item
        raise ValueError(f"unsupported policy decision: {value!r}")


@dataclass(frozen=True)
class PolicyEvaluation:
    decision: PolicyDecision
    reason: str | None = None
    rule_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW


@dataclass(frozen=True)
class PolicyRule:
    id: str
    org_id: str
    scope_type: str
    scope_id: str
    action: str
    resource: str
    effect: PolicyDecision
    conditions: dict
    priority: int
    created_at: str


class PolicyEvaluator:
    """Small enterprise policy evaluator.

    The first production slice supports explicit allow/deny/approval-required
    rules over action/resource plus optional scope and simple equality
    conditions. No matching rule means allow, preserving current HASHI behavior
    until administrators configure governed policies.
    """

    def __init__(self, store: EnterpriseStore, *, org_id: str):
        self.store = store
        self.store.init_schema()
        self.org_id = _require_id(org_id, "org_id")

    @classmethod
    def from_path(cls, db_path: Path | str, *, org_id: str) -> "PolicyEvaluator":
        return cls(EnterpriseStore(db_path), org_id=org_id)

    def add_rule(
        self,
        *,
        action: str,
        effect: PolicyDecision | str,
        resource: str = "*",
        scope_type: str = "org",
        scope_id: str | None = None,
        conditions: dict | None = None,
        priority: int = 100,
        rule_id: str | None = None,
    ) -> PolicyRule:
        action = _require_text(action, "action").lower()
        resource = _require_text(resource, "resource").lower()
        effect_value = PolicyDecision.from_value(effect.value if isinstance(effect, PolicyDecision) else str(effect))
        scope_type = _require_text(scope_type, "scope_type").lower()
        scope_id = _require_id(scope_id or self.org_id, "scope_id")
        rule_id = _require_id(rule_id or f"pol-{uuid4().hex}", "rule_id")
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO policy_rules(
                    id, org_id, scope_type, scope_id, action, resource, effect,
                    conditions_json, priority, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    self.org_id,
                    scope_type,
                    scope_id,
                    action,
                    resource,
                    effect_value.value,
                    json.dumps(conditions or {}, sort_keys=True),
                    int(priority),
                    now,
                ),
            )
        return self.get_rule(rule_id)

    def get_rule(self, rule_id: str) -> PolicyRule:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM policy_rules WHERE org_id = ? AND id = ?",
                (self.org_id, _require_id(rule_id, "rule_id")),
            ).fetchone()
        if row is None:
            raise ValueError(f"policy rule not found: {rule_id!r}")
        return _rule_from_row(row)

    def list_rules(self) -> list[PolicyRule]:
        with self.store.connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM policy_rules
                WHERE org_id = ?
                ORDER BY priority DESC, created_at ASC, id ASC
                """,
                (self.org_id,),
            ).fetchall()
        return [_rule_from_row(row) for row in rows]

    def evaluate(
        self,
        action: str,
        *,
        resource: str = "*",
        context: dict | None = None,
    ) -> PolicyEvaluation:
        action = _require_text(action, "action").lower()
        resource = _require_text(resource, "resource").lower()
        context = context or {}
        for rule in self.list_rules():
            if not _matches(rule.action, action):
                continue
            if not _matches(rule.resource, resource):
                continue
            if not _scope_matches(rule, context):
                continue
            if not _conditions_match(rule.conditions, context):
                continue
            return PolicyEvaluation(
                decision=rule.effect,
                reason=f"matched policy rule {rule.id}",
                rule_id=rule.id,
            )
        return PolicyEvaluation(
            decision=PolicyDecision.ALLOW,
            reason="default allow (no matching enterprise policy rule)",
        )


def evaluate_governance_policy(action: str, context: dict | None = None) -> PolicyEvaluation:
    context = context or {}
    global_config = context.get("global_config")
    profile = str(
        context.get("deployment_profile")
        or getattr(global_config, "deployment_profile", "personal")
        or "personal"
    )
    if profile == "personal":
        return PolicyEvaluation(PolicyDecision.ALLOW, reason="personal_profile")

    org_id = context.get("org_id") or getattr(global_config, "organization_id", None)
    if not org_id:
        return PolicyEvaluation(PolicyDecision.DENY, reason="missing_organization_id")

    db_path = context.get("db_path")
    if db_path is None:
        bridge_home = Path(context.get("bridge_home") or getattr(global_config, "bridge_home", "."))
        db_path = bridge_home / "state" / "enterprise.sqlite"

    evaluator = PolicyEvaluator.from_path(db_path, org_id=str(org_id))
    return evaluator.evaluate(
        action,
        resource=str(context.get("resource") or "*"),
        context=context,
    )


def _rule_from_row(row) -> PolicyRule:
    return PolicyRule(
        id=row["id"],
        org_id=row["org_id"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        action=row["action"],
        resource=row["resource"],
        effect=PolicyDecision.from_value(row["effect"]),
        conditions=json.loads(row["conditions_json"] or "{}"),
        priority=int(row["priority"]),
        created_at=row["created_at"],
    )


def _matches(pattern: str, value: str) -> bool:
    pattern = (pattern or "*").strip().lower()
    value = (value or "").strip().lower()
    return pattern == "*" or pattern == value


def _scope_matches(rule: PolicyRule, context: dict) -> bool:
    scope_type = (rule.scope_type or "org").lower()
    if scope_type == "org":
        return str(context.get("org_id") or "").strip() in {"", rule.scope_id}
    context_key = f"{scope_type}_id"
    return str(context.get(context_key) or "").strip() == rule.scope_id


def _conditions_match(conditions: dict, context: dict) -> bool:
    for key, expected in (conditions or {}).items():
        actual = context.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _require_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _require_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
