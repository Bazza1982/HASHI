from __future__ import annotations

import secrets
from dataclasses import dataclass
import re
from typing import Any

from orchestrator.enterprise.identity import EnterpriseRole, IdentityService, Project, User


SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


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

    def list_user_resources(
        self,
        *,
        org_id: str,
        filter_expression: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> dict[str, Any]:
        users = self.identity.list_users(org_id=org_id)
        filtered = filter_scim_users(users, filter_expression)
        return scim_list_response(
            filtered,
            start_index=start_index,
            count=count,
        )

    def get_user_resource(self, *, user_id: str) -> dict[str, Any]:
        user = self.identity.get_user(user_id)
        if user is None:
            raise ValueError(f"SCIM user not found: {user_id!r}")
        return scim_user_resource(user)

    def list_group_resources(
        self,
        *,
        org_id: str,
        filter_expression: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> dict[str, Any]:
        projects = self.identity.list_projects(org_id=org_id)
        filtered = filter_scim_groups(projects, filter_expression)
        return scim_group_list_response(
            filtered,
            self._users_by_project(org_id=org_id),
            start_index=start_index,
            count=count,
        )

    def get_group_resource(self, *, org_id: str, group_id: str) -> dict[str, Any]:
        project = self.identity.get_project(group_id)
        if project is None or project.org_id != org_id:
            raise ValueError(f"SCIM group not found: {group_id!r}")
        return scim_group_resource(project, self._users_by_project(org_id=org_id).get(project.id, []))

    def patch_user(
        self,
        *,
        user_id: str,
        payload: dict[str, Any],
    ) -> ScimProvisioningResult:
        user = self.identity.get_user(user_id)
        if user is None:
            raise ValueError(f"SCIM user not found: {user_id!r}")
        updates = extract_patch_updates(payload)
        action = "updated"
        if updates.get("active") is False:
            user = self.identity.deactivate_user(user_id=user.id)
            return ScimProvisioningResult(user=user, created=False, action="deactivated")
        display_name = updates.get("displayName")
        status = "active" if updates.get("active") is True else None
        user = self.identity.update_user_profile(
            user_id=user.id,
            display_name=display_name if isinstance(display_name, str) else None,
            status=status,
        )
        return ScimProvisioningResult(user=user, created=False, action=action)

    def _users_by_project(self, *, org_id: str) -> dict[str, list[User]]:
        result: dict[str, list[User]] = {}
        for user in self.identity.list_users(org_id=org_id):
            for membership in self.identity.list_project_memberships(user_id=user.id):
                project_id = str(membership.get("project_id") or "").strip()
                if project_id:
                    result.setdefault(project_id, []).append(user)
        return result


def scim_user_resource(user: User) -> dict[str, Any]:
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": user.id,
        "userName": user.email,
        "displayName": user.display_name,
        "name": {"formatted": user.display_name},
        "active": user.status == "active",
        "emails": [{"value": user.email, "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": user.created_at,
            "location": f"/scim/v2/Users/{user.id}",
        },
    }


def scim_group_resource(project: Project, members: list[User] | tuple[User, ...] | None = None) -> dict[str, Any]:
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": project.id,
        "displayName": project.name,
        "members": [
            {
                "value": user.id,
                "display": user.display_name,
                "$ref": f"/scim/v2/Users/{user.id}",
            }
            for user in sorted(members or [], key=lambda item: item.email)
            if user.status == "active"
        ],
        "meta": {
            "resourceType": "Group",
            "created": project.created_at,
            "location": f"/scim/v2/Groups/{project.id}",
        },
    }


def scim_list_response(
    users: list[User],
    *,
    start_index: int = 1,
    count: int = 100,
) -> dict[str, Any]:
    start = max(1, int(start_index or 1))
    page_size = max(0, int(count if count is not None else 100))
    offset = start - 1
    resources = users[offset : offset + page_size] if page_size else []
    return {
        "schemas": [SCIM_LIST_RESPONSE_SCHEMA],
        "totalResults": len(users),
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": [scim_user_resource(user) for user in resources],
    }


def scim_group_list_response(
    projects: list[Project],
    users_by_project: dict[str, list[User]],
    *,
    start_index: int = 1,
    count: int = 100,
) -> dict[str, Any]:
    start = max(1, int(start_index or 1))
    page_size = max(0, int(count if count is not None else 100))
    offset = start - 1
    resources = projects[offset : offset + page_size] if page_size else []
    return {
        "schemas": [SCIM_LIST_RESPONSE_SCHEMA],
        "totalResults": len(projects),
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": [scim_group_resource(project, users_by_project.get(project.id, [])) for project in resources],
    }


def filter_scim_users(users: list[User], filter_expression: str | None) -> list[User]:
    expression = str(filter_expression or "").strip()
    if not expression:
        return users
    match = re.fullmatch(r'(?i)\s*(userName|emails\.value)\s+eq\s+"([^"]+)"\s*', expression)
    if match:
        expected = match.group(2).strip().lower()
        return [user for user in users if user.email.lower() == expected]
    match = re.fullmatch(r"(?i)\s*active\s+eq\s+(true|false)\s*", expression)
    if match:
        expected_active = match.group(1).lower() == "true"
        return [user for user in users if (user.status == "active") is expected_active]
    raise ValueError(f"unsupported SCIM filter: {expression!r}")


def filter_scim_groups(projects: list[Project], filter_expression: str | None) -> list[Project]:
    expression = str(filter_expression or "").strip()
    if not expression:
        return projects
    match = re.fullmatch(r'(?i)\s*(id|displayName)\s+eq\s+"([^"]+)"\s*', expression)
    if match:
        field = match.group(1).lower()
        expected = match.group(2).strip().lower()
        if field == "id":
            return [project for project in projects if project.id.lower() == expected]
        return [project for project in projects if project.name.lower() == expected]
    raise ValueError(f"unsupported SCIM group filter: {expression!r}")


def extract_patch_updates(payload: dict[str, Any]) -> dict[str, Any]:
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("SCIM PATCH Operations are required")
    updates: dict[str, Any] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("SCIM PATCH operation must be an object")
        op = str(operation.get("op") or "").strip().lower()
        if op != "replace":
            raise ValueError(f"unsupported SCIM PATCH op: {op!r}")
        path = str(operation.get("path") or "").strip()
        value = operation.get("value")
        if path:
            _apply_patch_value(updates, path, value)
        elif isinstance(value, dict):
            for key, nested_value in value.items():
                _apply_patch_value(updates, str(key), nested_value)
        else:
            raise ValueError("SCIM PATCH replace requires path or object value")
    return updates


def _apply_patch_value(updates: dict[str, Any], path: str, value: Any) -> None:
    normalized = path.strip()
    key = normalized.lower()
    if key == "active":
        updates["active"] = _parse_bool(value, field="active")
        return
    if key in {"displayname", "name.formatted"}:
        text = _optional_text(value)
        if text:
            updates["displayName"] = text
        return
    raise ValueError(f"unsupported SCIM PATCH path: {path!r}")


def _parse_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"SCIM {field} must be a boolean")


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
