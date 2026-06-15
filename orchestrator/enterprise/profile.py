from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os


class DeploymentProfile(str, Enum):
    PERSONAL = "personal"
    TEAM = "team"
    ENTERPRISE = "enterprise"

    @classmethod
    def from_value(cls, value: str) -> "DeploymentProfile":
        normalized = (value or "").strip().lower()
        for item in cls:
            if item.value == normalized:
                return item
        raise ValueError(
            "Unsupported deployment_profile=%r. Supported: personal | team | enterprise" % value
        )


@dataclass(frozen=True)
class ProfileContext:
    profile: DeploymentProfile
    organization_id: str | None = None
    bootstrap_complete: bool = False

    @property
    def is_governed(self) -> bool:
        return self.profile != DeploymentProfile.PERSONAL


def resolve_deployment_profile(global_cfg: dict[str, object]) -> DeploymentProfile:
    raw = str(
        (global_cfg or {}).get("deployment_profile")
        or os.environ.get("HASHI_DEPLOYMENT_PROFILE", "")
    ).strip()
    if not raw:
        return DeploymentProfile.PERSONAL
    return DeploymentProfile.from_value(raw)


def parse_profile_context(global_cfg: dict[str, object]) -> ProfileContext:
    profile = resolve_deployment_profile(global_cfg)
    organization_id = (global_cfg or {}).get("organization_id")
    normalized_org_id = str(organization_id).strip() if organization_id not in (None, "") else None
    bootstrap_complete = bool((global_cfg or {}).get("enterprise_bootstrap_complete", False))
    return ProfileContext(profile=profile, organization_id=normalized_org_id or None, bootstrap_complete=bootstrap_complete)


def validate_profile_context(profile_ctx: ProfileContext) -> None:
    if not profile_ctx.is_governed:
        return

    if not profile_ctx.organization_id:
        raise ValueError(
            f"Governed profile '{profile_ctx.profile.value}' requires global.organization_id in agents.json."
            " This is the minimum bootstrap boundary for team/enterprise mode."
        )

    if not profile_ctx.bootstrap_complete:
        raise ValueError(
            "Governed profile requires enterprise/bootstrap initialization before startup. "
            "Please run the enterprise bootstrap flow to complete required setup."
        )
