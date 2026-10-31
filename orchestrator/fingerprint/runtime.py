"""Trusted PAO projections, frozen at admission and restored from the Run."""
from __future__ import annotations

from .models import FingerprintPolicy, RunIdentity, POLICY_KEY


def admission_policy(runtime) -> dict:
    config = getattr(runtime, "global_config", None)
    policy = getattr(config, "fingerprint", FingerprintPolicy())
    config_path = getattr(config, "config_path", None)
    if config_path is not None:
        from orchestrator.config_json import read_config_json
        policy = FingerprintPolicy.from_mapping(read_config_json(config_path).get("global", {}).get("fingerprint", {}))
    if not isinstance(policy, FingerprintPolicy):
        policy = FingerprintPolicy.from_mapping(policy)
    return policy.snapshot()


def context_policy(context: dict) -> FingerprintPolicy:
    return FingerprintPolicy.from_snapshot(context.get(POLICY_KEY))


def context_identity(context: dict) -> RunIdentity:
    return RunIdentity(
        str(context.get("instance_id") or ""),
        str(context.get("agent_name") or ""),
        str(context.get("hashi_session_id") or ""),
        str(context.get("hashi_run_id") or ""),
        str(context.get("request_id") or ""),
    )
