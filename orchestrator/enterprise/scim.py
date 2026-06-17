from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from orchestrator.enterprise.identity import EnterpriseRole, IdentityService, User


@dataclass(frozen=True)
class ScimProvisioningResult:
    user: User
    created: bool
    action: str
    external_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user.id,
            "email": self.user.email,
            "display_name": self.user.display_name,
            "status": self.user.status,
            "created": self.created,
            "action": self.action,
            "external_id": self.external_id,
        }


class ScimProvisioningService:
    """SCIM-style user provisioning primitive.

    This service intentionally implements the lifecycle core before exposing a
    full SCIM 2.0 HTTP surface. It supports enterprise IdP sync jobs that need
    deterministic create/update/deactivate semantics with HASHI identity state.
    """

    def __init__(self, identity: IdentityService):
        self.identity = identity

    def upsert_user(
        self,
        *,
        org_id: str,
        payload: dict[str, Any],
        default_project_id: str | None = None,
        default_role: EnterpriseRole | str = EnterpriseRole.INDIVIDUAL_USER,
    ) -> ScimProvisioningResult:
        email = _extract_email(payload)
        display_name = _extract_display_name(payload, email)
        active = bool(payload.get("active", True))
        external_id = _optional_text(payload.get("externalId"))
        existing = self.identity.get_user_by_email(org_id=org_id, email=email)
        status = "active" if active else "disabled"
        if existing is None:
            user = self.identity.create_user(
                org_id=org_id,
                email=email,
                display_name=display_name,
                password=secrets.token_urlsafe(32),
                status=status,
            )
            created = True
        else:
            user = self.identity.update_user_profile(
                user_id=existing.id,
                display_name=display_name,
                status=status,
            )
            created = False
        if not active:
            user = self.identity.deactivate_user(user_id=user.id)
            return ScimProvisioningResult(user=user, created=created, action="deactivated", external_id=external_id)
        if default_project_id:
            self.identity.assign_project_role(user_id=user.id, project_id=default_project_id, role=default_role)
        return ScimProvisioningResult(
            user=user,
            created=created,
            action="created" if created else "updated",
            external_id=external_id,
        )

    def deactivate_user(self, *, org_id: str, user_name: str) -> ScimProvisioningResult:
        user = self.identity.get_user_by_email(org_id=org_id, email=user_name)
        if user is None:
            raise ValueError(f"SCIM user not found: {user_name!r}")
        user = self.identity.deactivate_user(user_id=user.id)
        return ScimProvisioningResult(user=user, created=False, action="deactivated")


def _extract_email(payload: dict[str, Any]) -> str:
    user_name = _optional_text(payload.get("userName"))
    if user_name:
        return user_name
    emails = payload.get("emails")
    if isinstance(emails, list):
        for item in emails:
            if isinstance(item, dict):
                value = _optional_text(item.get("value"))
                if value:
                    return value
    raise ValueError("SCIM userName or emails[0].value is required")


def _extract_display_name(payload: dict[str, Any], fallback: str) -> str:
    display_name = _optional_text(payload.get("displayName"))
    if display_name:
        return display_name
    name = payload.get("name")
    if isinstance(name, dict):
        formatted = _optional_text(name.get("formatted"))
        if formatted:
            return formatted
        parts = [_optional_text(name.get(key)) for key in ("givenName", "familyName")]
        joined = " ".join(part for part in parts if part)
        if joined:
            return joined
    return fallback


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
