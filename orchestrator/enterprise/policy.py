from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.audit_schema import AuditEvent, AuditEventWriter
from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
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
    approval_request_id: str | None = None

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


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    org_id: str
    actor_id: str | None
    action: str
    resource: str
    status: str
    rule_id: str | None
    reason: str | None
    context: dict
    created_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None


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

    def create_approval_request(
        self,
        *,
        action: str,
        resource: str,
        context: dict | None = None,
        rule_id: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        action = _require_text(action, "action").lower()
        resource = _require_text(resource, "resource").lower()
        context = context or {}
        request_id = _require_id(request_id or f"appr-{uuid4().hex}", "request_id")
        actor_id = context.get("actor_id") or context.get("user_id")
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO approval_requests(
                    id, org_id, actor_id, action, resource, status, rule_id,
                    reason, context_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    self.org_id,
                    str(actor_id) if actor_id is not None else None,
                    action,
                    resource,
                    "pending",
                    rule_id,
                    reason,
                    json.dumps(_json_safe_context(context), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return self.get_approval_request(request_id)

    def get_approval_request(self, request_id: str) -> ApprovalRequest:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM approval_requests WHERE org_id = ? AND id = ?",
                (self.org_id, _require_id(request_id, "request_id")),
            ).fetchone()
        if row is None:
            raise ValueError(f"approval request not found: {request_id!r}")
        return _approval_from_row(row)

    def list_approval_requests(self, *, status: str | None = None) -> list[ApprovalRequest]:
        with self.store.connect() as con:
            if status is None:
                rows = con.execute(
                    """
                    SELECT *
                    FROM approval_requests
                    WHERE org_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (self.org_id,),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT *
                    FROM approval_requests
                    WHERE org_id = ? AND status = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (self.org_id, status),
                ).fetchall()
        return [_approval_from_row(row) for row in rows]


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
    evaluation = evaluator.evaluate(
        action,
        resource=str(context.get("resource") or "*"),
        context=context,
    )
    if evaluation.decision == PolicyDecision.APPROVAL_REQUIRED:
        approval = evaluator.create_approval_request(
            action=action,
            resource=str(context.get("resource") or "*"),
            context=context,
            rule_id=evaluation.rule_id,
            reason=evaluation.reason,
        )
        evaluation = PolicyEvaluation(
            decision=evaluation.decision,
            reason=evaluation.reason,
            rule_id=evaluation.rule_id,
            approval_request_id=approval.id,
        )
    if evaluation.decision != PolicyDecision.ALLOW:
        _write_policy_audit(
            bridge_home=Path(context.get("bridge_home") or getattr(global_config, "bridge_home", ".")),
            action=action,
            resource=str(context.get("resource") or "*"),
            evaluation=evaluation,
            context=context,
        )
    return evaluation


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


def _approval_from_row(row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        org_id=row["org_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        resource=row["resource"],
        status=row["status"],
        rule_id=row["rule_id"],
        reason=row["reason"],
        context=json.loads(row["context_json"] or "{}"),
        created_at=row["created_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
    )


def _write_policy_audit(
    *,
    bridge_home: Path,
    action: str,
    resource: str,
    evaluation: PolicyEvaluation,
    context: dict,
) -> None:
    status = "approval_required" if evaluation.decision == PolicyDecision.APPROVAL_REQUIRED else "denied"
    actor_id = context.get("actor_id") or context.get("user_id")
    audit_context = {
        "action": action,
        "resource": resource,
        "decision": evaluation.decision.value,
        "reason": evaluation.reason,
        "rule_id": evaluation.rule_id,
        "approval_request_id": evaluation.approval_request_id,
        **_json_safe_context(context),
    }
    writer = AuditEventWriter(
        enabled=True,
        jsonl_path=bridge_home / "state" / "enterprise_audit.jsonl",
    )
    writer.append(
        AuditEvent(
            event_type="policy",
            actor_id=actor_id,
            action=action,
            status=status,
            context=audit_context,
        )
    )
    org_id = context.get("org_id") or getattr(context.get("global_config"), "organization_id", None)
    if org_id:
        try:
            ledger = EnterpriseAuditLedger.from_path(bridge_home / "state" / "enterprise.sqlite", org_id=str(org_id))
            ledger.append(
                event_type="policy",
                actor_id=actor_id,
                action=action,
                status=status,
                project_id=context.get("project_id"),
                task_id=context.get("task_id"),
                request_id=context.get("request_id"),
                correlation_id=context.get("correlation_id"),
                context=audit_context,
            )
        except Exception:
            pass


def _json_safe_context(context: dict) -> dict:
    return {str(key): _json_safe_value(value) for key, value in (context or {}).items() if key != "global_config"}


def _json_safe_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    return repr(value)


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
