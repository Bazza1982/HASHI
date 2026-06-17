from __future__ import annotations

from dataclasses import dataclass

from orchestrator.enterprise.identity import EnterpriseRole, IdentityService, Session, User
from orchestrator.enterprise.oidc_exchange import OidcMappedIdentity


@dataclass(frozen=True)
class OidcSessionCompletion:
    user: User
    session: Session
    user_created: bool
    default_project_id: str | None
    default_role: str | None

    def public_payload(self) -> dict:
        return {
            "user": {
                "id": self.user.id,
                "org_id": self.user.org_id,
                "email": self.user.email,
                "display_name": self.user.display_name,
                "status": self.user.status,
            },
            "session": {
                "id": self.session.id,
                "token": self.session.token,
                "expires_at": self.session.expires_at,
            },
            "user_created": self.user_created,
            "default_project_id": self.default_project_id,
            "default_role": self.default_role,
        }


def complete_oidc_session(
    *,
    identity_service: IdentityService,
    mapped_identity: OidcMappedIdentity,
    default_project_id: str | None = None,
    default_role: EnterpriseRole | str = EnterpriseRole.INDIVIDUAL_USER,
    ttl_seconds: int = 86400,
) -> OidcSessionCompletion:
    role_value = default_role.value if isinstance(default_role, EnterpriseRole) else EnterpriseRole.from_value(str(default_role)).value
    user, created = identity_service.upsert_oidc_user(
        org_id=mapped_identity.org_id,
        email=mapped_identity.email,
        display_name=mapped_identity.display_name,
        default_project_id=default_project_id,
        default_role=role_value,
    )
    session = identity_service.create_session(user_id=user.id, ttl_seconds=ttl_seconds)
    return OidcSessionCompletion(
        user=user,
        session=session,
        user_created=created,
        default_project_id=default_project_id,
        default_role=role_value if default_project_id else None,
    )
