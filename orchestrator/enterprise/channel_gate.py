from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.enterprise.audit_schema import AuditEvent, AuditEventWriter
from orchestrator.enterprise.channels import ChannelPermission, ChannelRegistry
from orchestrator.enterprise.policy import evaluate_governance_policy


@dataclass(frozen=True)
class ChannelGateResult:
    allowed: bool
    reason: str
    channel_id: str | None = None


class EnterpriseChannelGate:
    def __init__(
        self,
        *,
        governed: bool,
        org_id: str | None,
        registry: ChannelRegistry | None = None,
        audit_writer: AuditEventWriter | None = None,
        global_config=None,
    ):
        self.governed = governed
        self.org_id = str(org_id or "").strip() or None
        self.registry = registry
        self.audit_writer = audit_writer or AuditEventWriter(enabled=False)
        self.global_config = global_config

    @classmethod
    def from_global_config(cls, global_config, *, audit_writer: AuditEventWriter | None = None) -> "EnterpriseChannelGate":
        profile = str(getattr(global_config, "deployment_profile", "personal") or "personal")
        governed = profile != "personal"
        org_id = str(getattr(global_config, "organization_id", "") or "").strip() or None
        registry = None
        if governed:
            bridge_home = Path(getattr(global_config, "bridge_home", None) or ".")
            registry = ChannelRegistry.from_path(bridge_home / "state" / "enterprise.sqlite")
        return cls(
            governed=governed,
            org_id=org_id,
            registry=registry,
            audit_writer=audit_writer,
            global_config=global_config,
        )

    def check_ingress(self, channel_type: str, **context) -> ChannelGateResult:
        return self.check(channel_type=channel_type, direction=ChannelPermission.INGRESS, **context)

    def check_egress(self, channel_type: str, **context) -> ChannelGateResult:
        return self.check(channel_type=channel_type, direction=ChannelPermission.EGRESS, **context)

    def check(
        self,
        *,
        channel_type: str,
        direction: ChannelPermission | str,
        actor_id: str | int | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        audit_context: dict | None = None,
    ) -> ChannelGateResult:
        if not self.governed:
            return ChannelGateResult(allowed=True, reason="personal_profile")
        if not self.org_id:
            result = ChannelGateResult(allowed=False, reason="missing_organization_id")
            self._audit_denial(channel_type, direction, actor_id, result, audit_context)
            return result
        if self.registry is None:
            result = ChannelGateResult(allowed=False, reason="channel_registry_unavailable")
            self._audit_denial(channel_type, direction, actor_id, result, audit_context)
            return result
        try:
            self.registry.ensure_default_channels(org_id=self.org_id)
            access = self.registry.check_access(
                org_id=self.org_id,
                channel_type=channel_type,
                direction=direction,
                user_id=user_id,
                team_id=team_id,
                project_id=project_id,
                agent_id=agent_id,
            )
        except Exception as exc:
            result = ChannelGateResult(allowed=False, reason=f"channel_gate_error:{type(exc).__name__}")
            self._audit_denial(channel_type, direction, actor_id, result, {"error": str(exc), **(audit_context or {})})
            return result
        result = ChannelGateResult(allowed=access.allowed, reason=access.reason, channel_id=access.channel_id)
        if not result.allowed:
            self._audit_denial(channel_type, direction, actor_id, result, audit_context)
            return result
        policy_result = self._check_policy(
            channel_type=channel_type,
            direction=direction,
            channel_id=result.channel_id,
            actor_id=actor_id,
            user_id=user_id,
            team_id=team_id,
            project_id=project_id,
            agent_id=agent_id,
        )
        if not policy_result.allowed:
            result = policy_result
            policy_context = {
                "policy_reason": policy_result.reason,
                **(audit_context or {}),
            }
            self._audit_denial(channel_type, direction, actor_id, result, policy_context)
        return result

    def _check_policy(
        self,
        *,
        channel_type: str,
        direction: ChannelPermission | str,
        channel_id: str | None,
        actor_id: str | int | None,
        user_id: str | None,
        team_id: str | None,
        project_id: str | None,
        agent_id: str | None,
    ) -> ChannelGateResult:
        direction_value = direction.value if isinstance(direction, ChannelPermission) else str(direction)
        try:
            evaluation = evaluate_governance_policy(
                "channel.access",
                {
                    "global_config": self.global_config,
                    "org_id": self.org_id,
                    "resource": f"channel:{channel_type}",
                    "channel_type": str(channel_type),
                    "direction": direction_value,
                    "actor_id": actor_id,
                    "user_id": user_id,
                    "team_id": team_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            )
        except Exception:
            return ChannelGateResult(allowed=False, reason="policy_error", channel_id=channel_id)
        if evaluation.allowed:
            return ChannelGateResult(allowed=True, reason="allowed", channel_id=channel_id)
        if evaluation.decision.value == "approval_required":
            return ChannelGateResult(allowed=False, reason="approval_required", channel_id=channel_id)
        return ChannelGateResult(allowed=False, reason="policy_denied", channel_id=channel_id)

    def _audit_denial(
        self,
        channel_type: str,
        direction: ChannelPermission | str,
        actor_id: str | int | None,
        result: ChannelGateResult,
        audit_context: dict | None,
    ) -> None:
        direction_value = direction.value if isinstance(direction, ChannelPermission) else str(direction)
        context = {
            "channel_type": str(channel_type),
            "direction": direction_value,
            "reason": result.reason,
            "channel_id": result.channel_id,
        }
        context.update(audit_context or {})
        self.audit_writer.append(
            AuditEvent(
                event_type="channel",
                actor_id=actor_id,
                action="channel_access",
                status="denied",
                context=context,
            )
        )
